# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The Components screen, the platform half (core/web/components_routes.py).

No database and no engine: the registry holds fake components declared
through the same @component decorator members.py uses - one per mode -
the journal is the in-memory stand-in with the contract's methods, the
reader is tests/test_web.py's fake and the audit sink records. Each test
pins one of the house rules the screen is built under: role dictates
visibility (both permissions, both enforced at the route), never assert
unverified state, aggregates drill down, one job at a time, the typed
phrase gates the apply, every transition on the trail, the dark scope
stays in its scope with the System section's palette and --navy
untouched, and no inline script.
"""

from __future__ import annotations

import html
import importlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core.web import components_routes as cx  # noqa: E402
from core.web import nav as product_nav  # noqa: E402
from test_web import _RecordingAudit, _client, _csrf  # noqa: E402

# The registry module itself (the package re-exports the registry()
# accessor under the same name, so the dotted import is the one that works).
reg = importlib.import_module("core.components.registry")

CSS = ROOT / "core" / "web" / "static" / "app.css"
TEMPLATE = ROOT / "core" / "web" / "templates" / "components.html"

# The System section's palette: the nine values the demonstration's .mm,
# .cp and .cx scopes carry, which the platform's .cx repeats.
PALETTE = {
    "--c-bg": "#0a1626", "--c-panel": "#0f2036", "--c-line": "#21344d", "--c-ink": "#dbe6f5",
    "--c-dim": "#7f93b0", "--c-ok": "#3ddc97", "--c-warn": "#ffb020", "--c-bad": "#ff6b57",
    "--c-accent": "#ff8a3d",
}

CHECKED = datetime.now(timezone.utc) - timedelta(hours=2)


# ---------------------------------------------------------------------------
# Fakes: three components through the registry's own decorator
# ---------------------------------------------------------------------------

def _procedure(direct: bool):
    def steps(ctx):
        return tuple(
            reg.Step(name, direct_capable=direct and name != "Plan",
                     instruction=f"do the {name.lower()} step",
                     verify="attest" if name in ("Back up", "Recover") else f"check {name.lower()}",
                     runbook="RUNBOOK_TEST")
            for name in reg.STEPS)
    return steps


@pytest.fixture
def fakes(monkeypatch):
    """One fake component per mode. The registry is emptied for the test and
    restored after it, and load_members() is pointed at the registry so
    members.py - present or not - never joins the fakes."""
    saved = dict(reg._REGISTRY)
    reg._REGISTRY.clear()
    monkeypatch.setattr(reg, "load_members", reg.registry)

    @reg.component(key="fake_direct", group="A", name="Fake image", kind="compose services",
                   mode="direct", backup_unit="The previous image digest, kept.",
                   recovery="compose up -d the previous digest, then the verify sweep.",
                   cadence_days=None, procedure=_procedure(True))
    def read_direct(ctx):
        running = reg.Fact("sha256:aaa", "docker inspect", CHECKED)
        built = reg.Fact("sha256:aaa", "BUILD.json", CHECKED)
        latest = reg.Fact("1.2.0", "manifest: registry", CHECKED, href="https://example.org/releases")
        return reg.Reading(key="fake_direct", running=running, built=built, latest=latest,
                           state=reg.decide_state(running, built, latest),
                           evidence=reg.Evidence(("Service", "Image"),
                                                 (("web", "sha256:aaa"), ("worker", "sha256:aaa"))))

    @reg.component(key="fake_guided", group="D", name="Fake ledger", kind="schema files",
                   mode="guided", backup_unit="pg_dump of the affected schemas.",
                   recovery="Run the down; restore the dump if the down cannot.",
                   cadence_days=None, procedure=_procedure(False))
    def read_guided(ctx):
        f = reg.Fact("7 files", "core/db/*.sql", CHECKED)
        return reg.Reading(key="fake_guided", running=f, built=f, latest=f,
                           state=reg.decide_state(f, f, f))

    @reg.component(key="fake_record", group="E", name="Fake gates", kind="gate record",
                   mode="record", backup_unit="n/a: a record, not a store.",
                   recovery="n/a: a build without green gates is not released.",
                   cadence_days=reg.CADENCE_DAYS["advisories"], procedure=_procedure(False))
    def read_record(ctx):
        running = reg.Fact("green", ".gates/last_green.json", CHECKED)
        built = reg.Fact("green", ".gates/last_green.json", CHECKED)
        latest = ctx.manifest_fact("fake_record")  # no manifest on a test host: unknown
        return reg.Reading(key="fake_record", running=running, built=built, latest=latest,
                           state=reg.decide_state(running, built, latest))

    yield {"fake_direct": "behind", "fake_guided": "current", "fake_record": "unknown"}
    reg._REGISTRY.clear()
    reg._REGISTRY.update(saved)


def _admin(roles="sysadmin"):
    audit = _RecordingAudit()
    client, _, _ = _client(roles=roles, audit=audit)
    journal = cx.MemoryJournal()
    client.app.state.components_journal = journal
    return client, journal, audit


def _post(client, path, data=None):
    payload = dict(data or {})
    payload["csrf_token"] = _csrf(client, "/system/components")
    return client.post(path, data=payload, follow_redirects=False)


def _start(client, key="fake_direct", target="1.2.0", mode=None):
    data = {"target": target}
    if mode:
        data["mode"] = mode
    return _post(client, f"/system/components/{key}/update", data)


def _confirm(client, job):
    return _post(client, f"/system/components/job/{job['id']}/confirm",
                 {"phrase": f"{job['component']} {job['target']}"})


def _step(client, job, step, outcome, **extra):
    data = {"step": step, "outcome": outcome, **extra}
    return _post(client, f"/system/components/job/{job['id']}/step", data)


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------

def test_the_screen_renders_for_an_admin_with_the_three_cards_and_every_group(fakes):
    client, _, audit = _admin()
    r = client.get("/system/components")
    assert r.status_code == 200
    body = html.unescape(r.text)  # the template escapes the apostrophe in "someone else's"
    for label in ("current — the rows below", "behind or drifted", "unknown"):
        assert label in body, label
    for title in reg.GROUPS.values():
        assert title in body, title
    for name in ("Fake image", "Fake ledger", "Fake gates"):
        assert name in body, name
    for heading in ("Manifest produced", "Backups and releases", "Update journal",
                    "Recent component actions", "Behind or drifted", "Restore rehearsed",
                    "No components in this group."):
        assert heading in body, heading
    assert "no patient dimension" in body
    assert any(e["action"] == "system.components_reviewed" and e["resource_key"] == "system/components"
               for e in audit.events)


def test_the_cards_count_the_states_and_open_to_their_rows(fakes):
    body = _admin()[0].get("/system/components").text
    assert '<div class="n good">1</div>' in body
    assert body.count('<div class="n warn">1</div>') == 2
    behind = body.split('id="cx-behind"')[1].split("</tr>")[0]
    unknown = body.split('id="cx-unknown"')[1].split("</tr>")[0]
    assert "Fake image" in behind and "Fake gates" not in behind
    assert "Fake gates" in unknown and "Fake image" not in unknown
    assert 'href="/system/components/fake_guided"' in body


def test_a_fact_never_checked_renders_amber_never_green(fakes):
    body = _admin()[0].get("/system/components/fake_record").text
    assert 'class="v-amber" title="manifest: never checked"' in body
    assert '<span class="v-amber">unknown</span>' in body
    assert '<span class="v-good">current</span>' not in body
    # And on the table: the manifest line is amber with no manifest at all.
    screen = _admin()[0].get("/system/components").text
    assert 'cx-meta v-amber' in screen and "Manifest produced never" in screen


def test_a_reader_role_is_refused_and_offered_no_nav_item(fakes):
    for roles in ("him", "viewer", "auditor"):
        audit = _RecordingAudit()
        client, _, _ = _client(roles=roles, audit=audit)
        assert client.get("/system/components").status_code == 403, roles
        assert client.get("/system/components/fake_direct").status_code == 403, roles
        assert any(e["action"] == "access.denied" and e["resource_key"] == "system:admin"
                   for e in audit.events), roles
        assert 'href="/system/components"' not in client.get("/").text, roles


def test_system_update_alone_opens_nothing(fakes):
    """admin holds system:update and not system:admin: both are checked at
    the route, so neither the read nor the apply is served."""
    from core.web.auth import PERMISSIONS, Role

    assert "system:update" in PERMISSIONS[Role.ADMIN]
    assert "system:admin" not in PERMISSIONS[Role.ADMIN]
    client, _, _ = _client(roles="admin")
    assert client.get("/system/components").status_code == 403
    token = _csrf(client, "/")
    r = client.post("/system/components/fake_direct/update",
                    data={"target": "1.2.0", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 403


def test_the_row_detail_renders_evidence_and_the_five_steps(fakes):
    client, _, audit = _admin()
    r = client.get("/system/components/fake_direct")
    assert r.status_code == 200
    body = r.text
    for heading in ("Evidence", "The five steps", "History", "Acknowledge"):
        assert heading in body, heading
    for name in reg.STEPS:
        assert f"<strong>{name}</strong>" in body, name
    assert "<th>Service</th>" in body and '<td class="mono">worker</td>' in body
    assert "Every row behind this reading (2)" in body
    assert "compose up -d the previous digest, then the verify sweep." in body
    assert "keep as is until" in body and 'name="until"' in body and 'name="reason"' in body
    assert "Apply update" in body and 'action="/system/components/fake_direct/update"' in body
    assert any(e["action"] == "system.components_reviewed"
               and e["resource_key"] == "system/components/fake_direct" for e in audit.events)


def test_an_unknown_key_is_not_found(fakes):
    client, _, _ = _admin()
    assert client.get("/system/components/nope").status_code == 404
    assert client.get("/system/components/fake_direct").status_code == 200


def test_the_screen_shares_the_demos_words(fakes):
    """Same experience, same words: the phrases the demonstration's view
    carries are on the platform screen."""
    body = html.unescape(_admin()[0].get("/system/components").text)
    for words in ("Every part of the platform that can go out of date, one row",
                  "what is running, what it was built from, the latest known",
                  "No update in flight", "Behind or drifted", "Unknown",
                  "<th>Component</th><th>Running</th><th>Built</th><th>Latest known</th>",
                  "<th>Source</th><th>Checked</th><th>State</th><th>Mode</th><th>Action</th>",
                  "Record only", "Apply update", "Open checklist",
                  "No releases recorded.", "No component actions yet.",
                  "audit trail's own clock"):
        assert words in body, words


# ---------------------------------------------------------------------------
# Acknowledgement
# ---------------------------------------------------------------------------

def test_acknowledgement_is_recorded_and_audited(fakes):
    client, journal, audit = _admin()
    until = (date.today() + timedelta(days=30)).isoformat()
    r = _post(client, "/system/components/fake_direct/acknowledge",
              {"until": until, "reason": "the release lands next sprint"})
    assert r.status_code == 303
    assert journal.acknowledgements("fake_direct")[0]["reason"] == "the release lands next sprint"
    entry = [e for e in audit.events if e["action"] == "system.component_acknowledged"]
    assert entry and entry[0]["resource_key"] == f"components/fake_direct until={until}"
    assert entry[0]["actor"] == "tester"
    body = client.get("/system/components/fake_direct").text
    assert "the release lands next sprint" in body and until in body
    # A past date or an empty reason is refused, and nothing is recorded.
    assert _post(client, "/system/components/fake_direct/acknowledge",
                 {"until": "2020-01-01", "reason": "x"}).status_code == 400
    assert _post(client, "/system/components/fake_direct/acknowledge",
                 {"until": until, "reason": "   "}).status_code == 400
    assert len(journal.acknowledgements("fake_direct")) == 1


# ---------------------------------------------------------------------------
# The update job
# ---------------------------------------------------------------------------

def test_starting_a_job_on_a_record_component_is_refused(fakes):
    client, journal, audit = _admin()
    r = _post(client, "/system/components/fake_record/update", {"target": "anything"})
    assert r.status_code == 400
    assert "record only" in r.text
    assert journal.open_job() is None
    assert any(e["action"] == "system.update_refused" for e in audit.events)
    # And a job with no target names nothing to move to.
    assert _post(client, "/system/components/fake_direct/update", {"target": " "}).status_code == 400
    assert journal.open_job() is None


def test_one_job_at_a_time(fakes):
    client, journal, audit = _admin()
    assert _start(client).status_code == 303
    job = journal.open_job()
    assert job and job["component"] == "fake_direct" and job["status"] == "planned"
    assert job["mode"] == "guided", "no updater is reachable, so a direct component runs guided"
    r = _post(client, "/system/components/fake_guided/update", {"target": "8 files"})
    assert r.status_code == 409
    assert "already open" in r.text and "fake_direct" in r.text
    assert journal.open_job()["id"] == job["id"]
    assert any(e["action"] == "system.update_refused" and e["resource_key"].endswith("job-open")
               for e in audit.events)
    body = client.get("/system/components").text
    assert "Update in flight" in body
    assert 'action="/system/components/fake_guided/update"' not in body


def test_direct_mode_needs_the_updater(fakes):
    client, journal, _ = _admin()

    class _Updater:
        def reachable(self):
            return True

    client.app.state.components_updater = _Updater()
    assert _start(client).status_code == 303
    assert journal.open_job()["mode"] == "direct"
    body = client.get("/system/components/fake_direct").text
    assert "the updater's turn" in body or "Confirm the plan" in body


def test_the_typed_phrase_gates_the_plan(fakes):
    client, journal, audit = _admin()
    assert _start(client).status_code == 303
    job = journal.open_job()
    body = client.get("/system/components/fake_direct").text
    assert "Confirm the plan" in body and "fake_direct 1.2.0" in body
    r = _post(client, f"/system/components/job/{job['id']}/confirm", {"phrase": "fake_direct 1.1.0"})
    assert r.status_code == 400 and "Not confirmed" in r.text
    assert journal.open_job()["status"] == "planned"
    assert _confirm(client, job).status_code == 303
    assert journal.open_job()["status"] == "confirmed"
    actions = [e["action"] for e in audit.events]
    for action in ("system.update_started", "system.update_refused", "system.update_confirmed"):
        assert action in actions, action
    # Only the open job answers to its id.
    assert _post(client, "/system/components/job/job_nope/confirm", {"phrase": "x"}).status_code == 404


def test_a_step_before_the_plan_is_confirmed_is_refused(fakes):
    client, journal, _ = _admin()
    assert _start(client).status_code == 303
    r = _step(client, journal.open_job(), "Apply", "ok")
    assert r.status_code == 400 and "Not recorded" in r.text


def test_a_failed_backup_fails_closed(fakes):
    client, journal, _ = _admin()
    assert _start(client).status_code == 303
    job = journal.open_job()
    assert _confirm(client, job).status_code == 303
    assert _step(client, job, "Back up", "failed").status_code == 303
    assert journal.open_job() is None
    assert journal.history("fake_direct")[0]["status"] == "failed"
    assert "failed at Back up" in client.get("/system/components/fake_direct").text


def test_a_failed_verify_shows_the_recover_step(fakes):
    client, journal, audit = _admin()
    assert _start(client).status_code == 303
    job = journal.open_job()
    assert _confirm(client, job).status_code == 303
    for name in ("Back up", "Apply"):
        assert _step(client, job, name, "ok", attested="1", evidence=f"{name} done").status_code == 303
    assert _step(client, job, "Verify", "failed", evidence="healthcheck red").status_code == 303
    job = journal.open_job()
    assert job["status"] == "verify_failed"
    assert cx.current_step(job) == (5, "Recover")
    body = client.get("/system/components/fake_direct").text
    assert "Step 5 of 5: Recover" in body
    assert "● waiting on you" in body and "✗ failed" in body
    assert 'name="step" value="Recover"' in body
    steps = [e for e in audit.events if e["action"] == "system.update_step"]
    assert len(steps) == 3
    assert any("Verify=failed" in e["resource_key"] for e in steps)
    assert any("Back up=ok" in e["resource_key"] and "attested" in e["resource_key"] for e in steps)
    # Recover, verified, closes the job as recovered - and the row's history says so.
    assert _step(client, job, "Recover", "ok", attested="1").status_code == 303
    assert journal.open_job() is None
    assert journal.history("fake_direct")[0]["status"] == "recovered"
    assert "rolled back and recovered" in client.get("/system/components/fake_direct").text


def test_a_passed_verify_completes_the_job_and_lands_in_the_journal(fakes):
    client, journal, _ = _admin()
    assert _start(client).status_code == 303
    job = journal.open_job()
    assert _confirm(client, job).status_code == 303
    for name in ("Back up", "Apply", "Verify"):
        assert _step(client, job, name, "ok", attested="1").status_code == 303
    assert journal.open_job() is None
    entry = journal.history("fake_direct")[0]
    assert entry["status"] == "done" and entry["outcome"] == "5 of 5 steps done, no recovery needed"
    body = client.get("/system/components").text
    assert "5 of 5 steps done, no recovery needed" in body and "update to 1.2.0" in body
    assert "No update in flight" in body


def test_the_banner_appears_on_every_page_while_a_job_is_open(fakes):
    client, journal, _ = _admin()
    assert "is in progress, step" not in client.get("/audit").text
    assert _start(client).status_code == 303
    body = client.get("/audit").text
    assert "An update of <strong>Fake image</strong> is in progress" in body
    assert "step 1 of 5" in body and "finish or roll back on" in body
    assert 'href="/system/components/fake_direct"' in body
    job = journal.open_job()
    assert _confirm(client, job).status_code == 303
    assert "step 2 of 5" in client.get("/audit").text
    assert _post(client, f"/system/components/job/{job['id']}/rollback").status_code == 303
    assert journal.open_job() is None
    assert "is in progress" not in client.get("/audit").text


def test_the_meta_refresh_is_present_only_while_a_job_is_open(fakes):
    client, journal, _ = _admin()
    assert 'http-equiv="refresh"' not in client.get("/system/components").text
    assert _start(client).status_code == 303
    assert 'http-equiv="refresh"' in client.get("/system/components").text
    assert 'http-equiv="refresh"' in client.get("/system/components/fake_guided").text


def test_a_restore_rehearsal_is_recorded_and_audited(fakes):
    client, journal, audit = _admin()
    slug = cx.store_slug("Index (Postgres)")
    assert _post(client, f"/system/components/backups/{slug}/rehearsed").status_code == 303
    row = next(b for b in journal.backups() if b["store"] == "Index (Postgres)")
    assert row["rehearsed_by"] == "tester" and row["rehearsed_at"]
    assert any(e["action"] == "system.restore_rehearsed" and e["resource_key"] == f"backups/{slug}"
               for e in audit.events)
    assert _post(client, "/system/components/backups/nope/rehearsed").status_code == 404
    body = client.get("/system/components").text
    assert "just now" in body and "by tester" in body


def test_the_journal_defaults_to_the_stand_in_when_the_engine_is_absent(fakes):
    client, _, _ = _client(roles="sysadmin")
    journal = client.app.state.components_journal
    for method in ("open_job", "start_job", "confirm_plan", "advance", "rollback", "finish",
                   "history", "acknowledge", "acknowledgements", "backups", "rehearsed", "releases"):
        assert callable(getattr(journal, method, None)), method
    try:
        import core.components.journal  # noqa: F401
    except ImportError:
        assert isinstance(journal, cx.MemoryJournal)
        assert "In memory" in client.get("/system/components").text


# ---------------------------------------------------------------------------
# Navigation, script, the dark scope
# ---------------------------------------------------------------------------

def test_the_system_group_is_control_panel_then_components_in_order(fakes):
    """The System group is the Control panel, then Components, last; on the
    demonstration Model monitoring sits between them, and it joins here the
    day it has a platform route (an entry with no route is a bug in the
    table). The rendered nav lists exactly the table's labels in order."""
    system = next(g for g in product_nav.NAV if g.label == "System")
    labels = [i.label for i in system.items]
    assert labels[0] == "Control panel" and labels[-1] == "Components", labels
    assert set(labels[1:-1]) <= {"Model monitoring"}, labels
    item = system.items[-1]
    assert (item.key, item.href, item.permissions) == ("components", "/system/components", ("system:admin",))
    body = _admin()[0].get("/").text
    block = re.search(r'>System</span>\s*<div class="nav-menu-drop">(.*?)</div>', body, re.S)
    assert block, "no System group in the rendered nav"
    assert re.findall(r'href="[^"]+">([^<]+)</a>', block.group(1)) == labels


def test_no_inline_script_on_the_screen(fakes):
    assert "<script" not in TEMPLATE.read_text(encoding="utf-8").lower()
    client, _, _ = _admin()
    r = client.get("/system/components")
    assert re.findall(r"<script[^>]*>", r.text) == ['<script src="/static/app.js" defer>']
    assert "script-src 'self'" in r.headers["content-security-policy"]


def _css_rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) for every rule, at-rule wrappers descended into and
    dropped, comments stripped first."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, sel, i, n = [], [], 0, len(css)
    while i < n:
        ch = css[i]
        if ch == "{":
            selector = " ".join("".join(sel).split())
            sel = []
            if selector.startswith("@"):
                i += 1
                continue
            j = css.index("}", i)
            rules.append((selector, css[i + 1:j]))
            i = j + 1
        elif ch == "}":
            sel = []
            i += 1
        else:
            sel.append(ch)
            i += 1
    return rules


def _tokens(body: str) -> dict[str, str]:
    return {k: " ".join(v.split()) for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


_CX = re.compile(r"\.cx(?![\w-])")


def test_every_cx_rule_in_app_css_is_scoped_under_cx():
    rules = _css_rules(CSS.read_text(encoding="utf-8"))
    cx_rules = [(sel, body) for sel, body in rules if _CX.search(sel)]
    assert len(cx_rules) > 40, "the .cx scope is missing or thin"
    unscoped = [sel for sel, _ in cx_rules
                if not all(re.match(r"^\.cx(?![\w-])", part.strip()) for part in sel.split(","))]
    assert not unscoped, unscoped


def _scope_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    for sel, body in _css_rules(CSS.read_text(encoding="utf-8")):
        if sel.strip() == ".cx":
            tokens.update(_tokens(body))
    return tokens


def test_the_dark_scope_repeats_the_control_panel_palette_and_never_sets_navy():
    tokens = _scope_tokens()
    for key, value in PALETTE.items():
        assert tokens.get(key) == value, (key, tokens.get(key))
    for sel, body in _css_rules(CSS.read_text(encoding="utf-8")):
        if _CX.search(sel):
            assert "--navy" not in _tokens(body), sel


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    rgb = tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_the_dark_scope_measures_its_contrast():
    """Measured, never eyeballed: every text token reads at least 4.5:1 on
    every ground it sits on, and every control edge at least 3:1."""
    t = _scope_tokens()
    grounds = (t["--c-bg"], t["--c-panel"], t["--wash"])
    for text in ("--c-ink", "--c-dim", "--c-ok", "--c-warn", "--c-bad", "--c-accent",
                 "--slate", "--mute", "--faint", "--body", "--good", "--warn-fg", "--amber"):
        for ground in grounds:
            assert _ratio(t[text], ground) >= 4.5, (text, ground, round(_ratio(t[text], ground), 2))
    for ground in grounds:
        assert _ratio(t["--c-ctrl"], ground) >= 3, ("--c-ctrl", ground)
        assert _ratio(t["--line-strong"], ground) >= 3, ("--line-strong", ground)
# Made by Ryan Gomez & Co. Inc.
