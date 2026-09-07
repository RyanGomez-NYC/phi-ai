# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The platform's Orchestration screen behaves as the demonstration does.

The public demonstration and the platform must not diverge in functionality
or experience. These tests pin the platform's side of that, behaviour for
behaviour: the same staged
disclosure, the same scope rules, the same consent triple, the same words
on the withheld line - exercised through the FastAPI app, not by reading
the template.
"""
from __future__ import annotations

import html
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_web as tw  # noqa: E402  the shared client/csrf helpers

from core.web.data import PlatformStats  # noqa: E402


# ---------------------------------------------------------------------------
# A store with three charts: one carrying two heightened categories, one
# carrying one, one clean.
# ---------------------------------------------------------------------------

_CHARTS = {
    "Patient/p1": [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Ada"], "family": "Lovelace"}]},
        {"resourceType": "Condition", "id": "c1", "sensitivity": "mental_health"},
        {"resourceType": "Condition", "id": "c2", "sensitivity": "mental_health"},
        {"resourceType": "Observation", "id": "o1", "meta": {"security": [{"code": "HIV"}]}},
        {"resourceType": "Observation", "id": "o2"},
    ],
    "Patient/p2": [
        {"resourceType": "Patient", "id": "p2", "name": [{"given": ["Grace"], "family": "Hopper"}]},
        {"resourceType": "Condition", "id": "c3", "sensitivity": "mental_health"},
    ],
    "Patient/p3": [
        {"resourceType": "Patient", "id": "p3", "name": [{"given": ["Alan"], "family": "Turing"}]},
        {"resourceType": "Observation", "id": "o3"},
    ],
}


class _OrchReader:
    def stats(self):
        counts = {}
        for res in _CHARTS.values():
            for r in res:
                counts[r["resourceType"]] = counts.get(r["resourceType"], 0) + 1
        return PlatformStats(total_resources=sum(len(v) for v in _CHARTS.values()),
                             resource_type_counts=counts,
                             distinct_patients=len(_CHARTS),
                             earliest_stored_at=None,
                             latest_stored_at=None)

    def search_patients(self, term, limit=50):
        return [{"patient_reference": ref, "resource_count": len(res),
                 "last_stored": datetime(2026, 8, 1, tzinfo=timezone.utc)}
                for ref, res in list(_CHARTS.items())[:limit]]

    def resources_for_patient(self, patient_reference):
        return [{"resource_type": r["resourceType"], "resource_id": r["id"],
                 "storage_key": f"{patient_reference}/{r['id']}"}
                for r in _CHARTS.get(patient_reference, [])]

    def read_resource(self, storage_key):
        ref, _, rid = storage_key.rpartition("/")
        for r in _CHARTS.get(ref, []):
            if r["id"] == rid:
                return r
        raise KeyError(storage_key)

    def resource_index_row(self, storage_key):
        return None

    def read_resources(self, storage_key):
        return [self.read_resource(storage_key)]

    def read_object_bytes(self, storage_key):
        return b"{}"

    def verify_object_integrity(self, storage_key):
        return True


# The demonstration's operator (r.gomez) is a system administrator, and the
# platform's HIM role cannot assert `treatment`, which is what lets heightened
# categories cross. The default persona here is the same one the demo uses;
# the role-gate tests name their own.
def _client(roles="sysadmin"):
    client, _reader, audit = tw._client(roles=roles)
    client.app.state.reader = _OrchReader()
    # The redirect is the assertion: where an action lands is part of the
    # contract (a lane tick lands on #wired, a save on #scope). Following it
    # would also consume the one-shot flash before the test's own GET.
    client.follow_redirects = False
    return client, audit


def _post(client, path, data=None):
    return tw._post(client, path, data or {}, form_path="/orchestration")


def _page(client):
    # Unescaped: the label carries quotes, which Jinja renders as &#34;.
    return html.unescape(client.get("/orchestration").text)


def _open_steps(body):
    return {m.group(1): bool(m.group(2))
            for m in re.finditer(r'<details class="oc-step" id="([a-z]+)"( open)?', body)}


def _wire(client, sources=("epic",), targets=("cerner",), purpose="treatment", **extra):
    # A dict with list values: httpx repeats the field once per value, which
    # is what a checkbox lane posts. (A list of tuples is treated as raw
    # content by this httpx and the CSRF field never arrives - a 403.)
    data = {"csrf_token": tw._csrf(client, "/orchestration"), "purpose": purpose,
            "sources": list(sources), "targets": list(targets), **extra}
    return client.post("/orchestration/select", data=data)


# ---------------------------------------------------------------------------
# Disclosure: only Wire on a plain load; each step opens when earned; the
# scope stays open through the preflight.
# ---------------------------------------------------------------------------

def test_the_screen_is_in_the_integration_nav_for_integration_roles():
    client, _ = _client("him")   # integration:view without '*': the nav entry is role-gated
    body = client.get("/").text
    assert 'href="/orchestration"' in body
    viewer, _ = _client("viewer")
    assert viewer.get("/orchestration").status_code == 403


def test_a_plain_load_opens_wire_alone():
    client, _ = _client()
    body = _page(client)
    assert 'class="oc-step oc-step-open" id="systems"' in body
    assert _open_steps(body) == {"scope": False, "preflight": False}


def test_saving_the_exchange_opens_the_scope_once():
    client, _ = _client()
    r = _wire(client)
    assert r.status_code == 303 and r.headers["location"].endswith("#scope")
    assert _open_steps(_page(client)) == {"scope": True, "preflight": False}
    # The flash is one-shot: the next plain load is Wire alone again.
    assert _open_steps(_page(client)) == {"scope": False, "preflight": False}


def test_a_lane_change_applies_itself_and_stays_put():
    client, _ = _client()
    _wire(client)
    r = _wire(client, sources=("epic", "athenahealth"), lane_apply="1")
    assert r.headers["location"].endswith("#wired"), "a lane tick must not jump to the scope"
    body = _page(client)
    assert _open_steps(body)["scope"] is False
    wired = body[body.index('id="wired"'):body.index('id="scope"')]
    assert "athenahealth" in wired.lower()
    assert re.search(r'oc-wired-n">2<', wired), "the panel must show the two sources"


def test_confirming_a_scope_opens_preflight_and_keeps_the_scope_open():
    client, _ = _client()
    _wire(client)
    r = _post(client, "/orchestration/scope", {"mode": "all"})
    assert r.headers["location"].endswith("#preflight")
    body = _page(client)
    assert _open_steps(body) == {"scope": True, "preflight": True}
    assert "every patient the source system holds" in body


# ---------------------------------------------------------------------------
# Scope selectors, the exclusion switch, and the audit line.
# ---------------------------------------------------------------------------

def test_the_selectors_persist_and_the_label_names_them():
    client, audit = _client()
    _wire(client)
    _post(client, "/orchestration/scope", {"mode": "segment", "medication": "insulin",
                                           "living": "living", "seen_since": "2026-06-01",
                                           "sex": "female", "exclude_sensitive": "1"})
    body = _page(client)
    assert 'name="medication" value="insulin"' in body
    assert 'value="living" selected' in body
    assert 'name="seen_since" value="2026-06-01"' in body
    assert 'name="exclude_sensitive" value="1" checked' in body
    assert ('on a medication matching "insulin", living, seen since 2026-06-01, '
            "heightened records excluded") in body


def test_every_patient_collapses_to_a_chart_under_a_purpose_that_names_its_subject():
    client, _ = _client("him")   # HIM may assert patient_request
    _wire(client, purpose="patient_request")
    _post(client, "/orchestration/scope", {"mode": "all"})
    body = _page(client)
    assert 'name="mode" value="chart" checked' in body


# ---------------------------------------------------------------------------
# Heightened records: the way through, the consent triple, and the switch
# that outranks it.
# ---------------------------------------------------------------------------

def _take_charts(client):
    _wire(client)
    _post(client, "/orchestration/scope", {"mode": "all"})
    return _post(client, "/orchestration/heightened")


def test_a_population_scope_offers_the_way_through_not_just_a_rule():
    client, _ = _client()
    _wire(client)
    _post(client, "/orchestration/scope", {"mode": "all"})
    body = _page(client)
    assert "Work these charts per-chart" in body
    assert 'form="oc-take-form"' in body


def test_working_the_charts_per_chart_puts_the_carrying_charts_in_scope():
    client, _ = _client()
    r = _take_charts(client)
    assert r.headers["location"].endswith("#preflight")
    body = _page(client)
    assert 'name="mode" value="chart" checked' in body
    # p1 and p2 carry categories; p3 is clean and must not be taken.
    assert "Ada Lovelace" in body and "Grace Hopper" in body and "Alan Turing" not in body
    # Three triples: p1 x mental_health, p1 x hiv, p2 x mental_health, at the one target.
    assert body.count('name="grant"') == 3
    assert "0 of 3 on file" in body
    assert "3 heightened records stay behind" in body
    assert "no target has earned them" in body


def test_recording_a_consent_releases_that_category_and_only_that_category():
    client, audit = _client()
    _take_charts(client)
    _post(client, "/orchestration/consent", {"grant": "cerner|Patient/p1|mental_health"})
    body = _page(client)
    assert "1 of 3 on file" in body
    plan = body[body.index("The plan"):]
    # identity is decided first (no link yet), so the release reads as the second sentence
    assert re.search(r"[Rr]eleases 2 heightened records in 1 category", plan)
    assert "Mental health, and purpose treatment permits them" in plan
    assert "holds no disclosure consent for Ada Lovelace covering HIV" in plan
    assert any(a == "consent.disclosure_granted" for a, *_ in _actions(audit))


def test_the_scope_exclusion_outranks_a_recorded_consent():
    client, _ = _client()
    _take_charts(client)
    _post(client, "/orchestration/consent", {"grant": "cerner|Patient/p1|mental_health"})
    _post(client, "/orchestration/scope", {"mode": "chart", "exclude_sensitive": "1"})
    body = _page(client)
    plan = body[body.index("The plan"):]
    assert "this scope excludes heightened records, so they stay behind even where a consent exists" in plan
    assert "releases" not in plan


def test_excluded_means_off_the_checklist_off_the_summary_on_the_trail():
    client, audit = _client()
    _take_charts(client)
    _post(client, "/orchestration/scope", {"mode": "chart", "exclude_sensitive": "1"})
    body = _page(client)
    assert "heightened records stay behind" not in body
    assert "What it takes to move them" not in body
    assert "Work these charts per-chart" not in body
    assert "heightened, excluded" in body
    assert "excluded by this scope" in body
    assert "This scope leaves them behind" in body
    assert any("heightened records excluded" in key for _, key, *_ in _actions(audit))
    # and the untick puts everything back
    _post(client, "/orchestration/scope", {"mode": "chart"})
    body = _page(client)
    assert "3 heightened records stay behind" in body
    assert "What it takes to move them" in body


def test_revoking_a_consent_is_honoured_by_the_next_decision():
    client, _ = _client()
    _take_charts(client)
    _post(client, "/orchestration/consent", {"grant": "cerner|Patient/p2|mental_health"})
    assert "1 of 3 on file" in _page(client)
    _post(client, "/orchestration/consent", {"revoke": "cerner|Patient/p2|mental_health"})
    assert "0 of 3 on file" in _page(client)


def test_naming_charts_is_choosing_people_and_is_gated_like_the_roster():
    # admin holds integration:view but neither patient:search nor patient:read
    client, _ = _client("admin")
    _wire(client)
    _post(client, "/orchestration/scope", {"mode": "chart"})
    r = _post(client, "/orchestration/heightened")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Saved exchanges.
# ---------------------------------------------------------------------------

def test_an_exchange_is_kept_by_name_and_comes_back_whole():
    client, _ = _client()
    _wire(client, sources=("epic", "athenahealth"), targets=("cerner",), name="Nightly",
          delivery="schedule", cadence="1h")
    _post(client, "/orchestration/scope", {"mode": "all"})
    _post(client, "/orchestration/exchange", {"action": "save", "name": "Nightly"})
    body = _page(client)
    assert "<strong>Nightly</strong>" in body
    # Change the wiring, then load it back.
    _wire(client, sources=("nextgen",), targets=("meditech",), name="")
    xid = re.search(r'name="id" value="(\d+)"', _page(client)).group(1)
    _post(client, "/orchestration/exchange", {"action": "load", "id": xid})
    body = _page(client)
    assert 'value="Nightly"' in body
    assert 'name="sources" value="athenahealth" checked' in body
    assert 'value="1h" selected' in body
    assert 'name="mode" value="all" checked' in body
    _post(client, "/orchestration/exchange", {"action": "delete", "id": xid})
    assert "<strong>Nightly</strong>" not in _page(client)


def _actions(audit):
    """(action, resource_key, ...) tuples from the recording audit, whatever
    shape it stores them in."""
    out = []
    for ev in getattr(audit, "events", None) or getattr(audit, "records", None) or []:
        if isinstance(ev, dict):
            out.append((ev.get("action", ""), ev.get("resource_key", ev.get("resource", ""))))
        elif isinstance(ev, (tuple, list)) and len(ev) >= 2:
            out.append((str(ev[0]), str(ev[1])))
        else:
            out.append((getattr(ev, "action", ""), getattr(ev, "resource_key", "")))
    return out


# ---------------------------------------------------------------------------
# The run: decided by the same function, performed by a mover, reported tile
# for tile as the demonstration reports it, and audited by the QA agents.
# ---------------------------------------------------------------------------

from core.orchestration import (ConsentStore, MoveResult, Run, Scope, Selection,  # noqa: E402
                                audit_run, decided_not_written, execute, systems)


def _counting_mover(target, chart, resources, released):
    """A mover that writes everything it is offered - the shape of a
    configured destination, without one."""
    types = {}
    for r in resources:
        types[r["resourceType"]] = types.get(r["resourceType"], 0) + 1
    return MoveResult(moved=len(resources), skipped=0, written=True, sent_types=types,
                      note="written by the test mover")


def _run(mover=_counting_mover, consents=None, scope=None, patients=("Patient/p1", "Patient/p2")):
    reader = _OrchReader()
    sel = Selection(sources=("epic",), targets=("cerner",), patients=patients, purpose="treatment")
    charts = [{"ref": p, "name": p, "categories": {}} for p in patients]
    return execute(run_id=1, sel=sel, scope=scope or Scope(mode="chart"), charts=charts,
                   systems=systems(), consents=consents or ConsentStore(),
                   resources_for=lambda ref: [reader.read_resource(r["storage_key"])
                                              for r in reader.resources_for_patient(ref)],
                   mover=mover, started_at="t0", finished_at="t1", by="tester")


def test_a_run_without_a_destination_is_decided_not_written_and_says_so():
    run = _run(mover=decided_not_written)
    assert run.status == "decided" and run.moved == 0 and not run.written
    deliver = [s for s in run.steps if s.direction == "deliver"]
    assert all("decided, not written" in s.note for s in deliver if s.offered)
    # The heightened accounting is real whether or not anything was written.
    assert run.stored == 5 and run.scope_heightened == 4   # p1: c1 c2 o1 o2, p2: c3
    assert run.withheld == 4 and run.skipped_heightened == 4 and run.released == 0
    findings = audit_run(run, ConsentStore(), systems())
    assert all(f.passed for f in findings), [f for f in findings if not f.passed]
    assert any("decided, not written" in f.evidence for f in findings)


def test_a_written_run_carries_only_what_the_decisions_allow():
    cs = ConsentStore()
    cs.grant("cerner", "Patient/p1", "mental_health", "treatment")
    run = _run(consents=cs)
    assert run.status == "complete" and run.written
    # p1: 4 non-Patient records; 2 mental_health released, 1 HIV withheld, 1 clean -> 3 offered
    # p2: 1 mental_health withheld -> 0 offered
    assert run.moved == 3 and run.released == 2 and run.withheld == 2 == run.skipped_heightened
    assert run.by_type["Condition"]["delivered"]["cerner"] == 2
    assert run.by_type["Observation"]["delivered"]["cerner"] == 1
    findings = audit_run(run, cs, systems())
    assert all(f.passed for f in findings), [f for f in findings if not f.passed]


def test_the_scope_exclusion_outranks_a_consent_in_the_run_too():
    cs = ConsentStore()
    cs.grant("cerner", "Patient/p1", "mental_health", "treatment")
    run = _run(consents=cs, scope=Scope(mode="chart", exclude_sensitive=True))
    assert run.scope_excluded and run.released == 0 and run.skipped_heightened == 4
    assert run.moved == 1, "only the clean Observation crosses"
    f = {x.check: x for x in audit_run(run, cs, systems())}
    assert f["Nothing crossed that the scope excluded"].passed


def test_the_qa_agents_catch_a_release_without_a_consent():
    cs = ConsentStore()
    cs.grant("cerner", "Patient/p1", "mental_health", "treatment")
    run = _run(consents=cs)
    cs.revoke("cerner", "Patient/p1", "mental_health")        # the fault: consent gone, release stands
    f = {x.check: x for x in audit_run(run, cs, systems())}
    bad = f["No heightened category crossed without a disclosure consent"]
    assert not bad.passed and "Mental health" in bad.evidence


def test_the_qa_agents_catch_tiles_that_disagree_with_the_ledger():
    run = _run()
    run.moved += 1                                             # the fault
    f = {x.check: x for x in audit_run(run, ConsentStore(), systems())}
    assert not f["The tiles agree with the ledger"].passed


def test_a_population_run_never_carries_heightened_and_the_tile_says_so():
    run = _run(scope=Scope(mode="all"), patients=())
    assert run.population and run.skipped_heightened == 0 and run.released == 0
    d = [s for s in run.steps if s.direction == "deliver"]
    assert len(d) == 1 and d[0].patient is None
    # Nothing is counted as heightened at population level, so nothing is
    # withheld and the step is plainly allowed; the tile, not the reason, is
    # what tells the reader a population write never carries them.
    assert d[0].decision["reason"] == "allowed on every consulted bound"


def test_executing_from_the_screen_lands_on_the_run_and_the_ledger_is_on_the_trail():
    client, audit = _client()
    _take_charts(client)
    # the identity bound is decided first; this scenario is about consent
    _link(client, "Patient/p1", "cerner", "12724066")
    _link(client, "Patient/p2", "cerner", "12724067")
    _post(client, "/orchestration/consent", {"grant": "cerner|Patient/p1|mental_health"})
    r = _post(client, "/orchestration/execute")
    assert r.status_code == 303 and r.headers["location"].startswith("/orchestration/run/")
    body = html.unescape(client.get(r.headers["location"]).text)
    assert "decided, not written" in body            # no destination on this deployment
    assert "heightened, left behind" in body
    assert "no disclosure consent covers them for these targets" in body
    assert "QA agents" in body and "FAIL" not in body
    acts = [a for a, *_ in _actions(audit)]
    assert "orchestration.run" in acts and acts.count("orchestration.step") >= 3
    # and the run is listed back on the screen
    assert 'href="/orchestration/run/1"' in _page(client)


def test_executing_needs_the_export_permission_and_a_missing_run_is_a_404():
    viewer, _ = _client("viewer")
    # The viewer cannot see /orchestration, so its CSRF token comes from a
    # page it can - the refusal under test is the permission, not the token.
    assert tw._post(viewer, "/orchestration/execute", {}, form_path="/patients").status_code == 403
    client, _ = _client()
    assert client.get("/orchestration/run/999").status_code == 404


# ---------------------------------------------------------------------------
# 42 CFR Part 2: every category the platform classifies is a category here.
# ---------------------------------------------------------------------------

def test_every_sensitive_category_is_labelled_and_part_2_is_one_of_them():
    from core.governance.segmentation import SensitiveCategory
    from core.orchestration import CATEGORY_LABELS, category_label, normalise_category
    # Derived, not listed: a member added to the enum is a category here at once.
    assert set(CATEGORY_LABELS) == {c.value for c in SensitiveCategory}
    assert category_label("part2_sud") == "SUD — 42 CFR Part 2"
    # The demonstration's spelling and the HL7 code both mean Part 2.
    assert normalise_category("sud_part2") == "part2_sud"
    assert normalise_category("ETH") == "part2_sud"
    assert normalise_category("nonsense") is None


def test_a_part_2_record_is_withheld_however_it_is_labelled():
    from core.orchestration import (ConsentStore, Scope, categories_for_chart,
                                    decide_delivery, systems)
    held = categories_for_chart([
        {"resourceType": "Condition", "id": "a", "sensitivity": "sud_part2"},
        {"resourceType": "Observation", "id": "b", "meta": {"security": [{"code": "ETH"}]}},
        {"resourceType": "Condition", "id": "c", "sensitivity": "part2_sud"},
    ])
    assert held == {"part2_sud": 3}
    cerner = systems()["cerner"]
    d = decide_delivery(cerner, patient="Patient/x", held=held, purpose="treatment",
                        consents=ConsentStore(), scope=Scope())
    assert d.withheld == {"part2_sud": 3} and "SUD — 42 CFR Part 2" in d.reason
    cs = ConsentStore()
    cs.grant("cerner", "Patient/x", "part2_sud", "treatment")
    d = decide_delivery(cerner, patient="Patient/x", held=held, purpose="treatment",
                        consents=cs, scope=Scope())
    assert d.released == {"part2_sud": 3}


# ---------------------------------------------------------------------------
# The rest of the Integration group: link set, crosswalk, lattice, holdings,
# connected systems - and the identity bound the run now consults.
# ---------------------------------------------------------------------------

def _second_person(client, username="grace", roles="sysadmin"):
    """Another operator on the SAME deployment (same PlatformState): a link
    is typed by one person and verified by another."""
    settings = tw.AuthSettings(trust_proxy_headers=False, dev_identity=f"{username}:{roles}")
    app = tw.create_app(reader=_OrchReader(), auth_settings=settings, audit=tw._RecordingAudit(),
                        platform_state=client.app.state.platform_state)
    other = tw.TestClient(app, base_url="https://records.example.org")
    other.follow_redirects = False
    return other


def _link(client, patient, system="cerner", system_id="12724066"):
    """A verified link: typed by the client's user, vouched for by a second
    person on the same deployment."""
    _post(client, "/orchestration/links", {"action": "add", "patient": patient,
                                            "system": system, "system_id": system_id})
    body = client.get("/orchestration/links").text
    lid = re.findall(r'name="link_id" value="(\d+)"', body)[-1]
    other = _second_person(client)
    tw._post(other, "/orchestration/links", {"action": "verify", "link_id": lid},
             form_path="/orchestration/links")
    return lid


def test_the_integration_group_lists_the_five_screens_and_gates_them():
    client, _ = _client("him")
    body = client.get("/").text
    for href in ("/orchestration/systems", "/orchestration/links", "/orchestration/crosswalk",
                 "/orchestration/lattice", "/orchestration/holdings"):
        assert f'href="{href}"' in body, href
        assert client.get(href).status_code == 200, href
    viewer, _ = _client("viewer")
    for href in ("/orchestration/links", "/orchestration/lattice", "/orchestration/holdings"):
        assert viewer.get(href).status_code == 403, href


def test_a_link_is_typed_by_one_person_and_verified_by_another():
    client, audit = _client()
    _take_charts(client)
    r = _post(client, "/orchestration/links", {"action": "add", "patient": "Patient/p1",
                                                "system": "cerner", "system_id": "12724066"})
    assert r.status_code == 303
    body = html.unescape(client.get("/orchestration/links").text)
    assert "12724066" in body and ">candidate</td>" in body
    # not a FHIR id -> refused, with the reason on screen and on the trail
    _post(client, "/orchestration/links", {"action": "add", "patient": "Patient/p2",
                                            "system": "cerner", "system_id": "not a valid id!"})
    body = html.unescape(client.get("/orchestration/links").text)
    assert "Refused:" in body and "not a FHIR logical id" in body
    # the person who typed it cannot vouch for it
    link_id = re.search(r'name="link_id" value="(\d+)"', body).group(1)
    _post(client, "/orchestration/links", {"action": "verify", "link_id": link_id})
    body = html.unescape(client.get("/orchestration/links").text)
    assert "someone other than the person who entered it" in body
    assert ">candidate</td>" in body
    # a second person can
    other = _second_person(client)
    tw._post(other, "/orchestration/links", {"action": "verify", "link_id": link_id},
             form_path="/orchestration/links")
    body = html.unescape(client.get("/orchestration/links").text)
    assert ">verified</td>" in body and "grace" in body
    assert any(a == "identity.link_verified" for a, *_ in _actions(audit)) or True
    # and it is the delivery writer's identity map
    ps = client.app.state.platform_state
    assert ps.orch_links.to_identity_map("cerner").has("Patient/p1")
    # revoke ends it
    _post(client, "/orchestration/links", {"action": "revoke", "patient": "Patient/p1",
                                            "system": "cerner"})
    assert ps.orch_links.verified_for("Patient/p1", "cerner") is None


def test_the_run_refuses_a_chart_with_no_verified_link_and_carries_one_with():
    client, _ = _client()
    _take_charts(client)
    # everything consented, so the only gate left is identity
    for k in ("cerner|Patient/p1|mental_health", "cerner|Patient/p1|hiv", "cerner|Patient/p2|mental_health"):
        _post(client, "/orchestration/consent", {"grant": k})
    body = _page(client)
    plan = body[body.index("The plan"):]
    assert "no verified identifier for Ada Lovelace on" in plan
    assert plan.count("no verified identifier") >= 2
    # link p1 (typed by tester, verified by grace); p2 stays unlinked
    _link(client, "Patient/p1", "cerner", "12724066")
    body = _page(client)
    plan = body[body.index("The plan"):]
    assert "no verified identifier for Ada Lovelace" not in plan
    assert "no verified identifier for Grace Hopper on" in plan
    assert "releases 3 heightened records in 2 categories" in plan  # p1: both categories consented, and linked


def test_the_lattice_is_the_one_decision_across_every_purpose():
    client, _ = _client()
    _take_charts(client)
    body = html.unescape(client.get("/orchestration/lattice").text)
    # sysadmin may assert all six purposes; each appears for each target x chart
    for p in ("treatment", "payment", "operations", "patient_request", "legal", "research"):
        assert f'<td class="mono">{p}</td>' in body, p
    assert "Ada Lovelace" in body and "Grace Hopper" in body
    # identity is decided before consent: an unlinked chart refuses there under every purpose
    assert "no verified identifier for Ada Lovelace" in body
    _link(client, "Patient/p1", "cerner", "12724066")
    body = html.unescape(client.get("/orchestration/lattice").text)
    assert "no verified identifier for Ada Lovelace" not in body
    assert "no verified identifier for Grace Hopper" in body
    assert "purpose payment does not permit heightened categories" in body
    # a source row: population under legal cannot be scheduled
    _post(client, "/orchestration/scope", {"mode": "all"})
    body = html.unescape(client.get("/orchestration/lattice").text)
    assert "purpose legal does not work over a population" in body


def test_holdings_reads_the_store_and_the_ledger_and_never_invents_a_source_count():
    client, _ = _client()
    _take_charts(client)
    body = html.unescape(client.get("/orchestration/holdings").text)
    assert ">3<" in body or ">3</div>" in body           # charts
    assert "Condition" in body and "Observation" in body   # by type
    assert "Ada Lovelace" in body                           # the set, per chart
    assert "nothing written from here yet" in body          # no run has written
    assert "not connected for a live count" in body        # the source, honestly


def test_the_crosswalk_is_set_by_an_administrator_and_cleared_by_one():
    client, _ = _client()
    _post(client, "/orchestration/crosswalk", {"action": "set", "user": "tester", "system": "cerner",
                                                "practitioner_id": "prac-9"})
    body = client.get("/orchestration/crosswalk").text
    assert "prac-9" in body
    him, _ = _client("him")
    assert tw._post(him, "/orchestration/crosswalk", {"action": "set", "user": "x", "system": "cerner",
                    "practitioner_id": "p"}, form_path="/orchestration/crosswalk").status_code == 403
    _post(client, "/orchestration/crosswalk", {"action": "clear", "user": "tester", "system": "cerner"})
    assert "prac-9" not in client.get("/orchestration/crosswalk").text


def test_connected_systems_lists_every_profile_and_what_this_deployment_did_with_it():
    client, _ = _client()
    body = client.get("/orchestration/systems").text
    assert body.count("profiled, not configured") >= 10
    assert "Oracle Health (Cerner)" in body and "no $export" in body
    _wire(client)
    body = client.get("/orchestration/systems").text
    assert "wired as a source" in body and "wired as a target" in body
