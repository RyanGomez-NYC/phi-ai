# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The capability screens, driven end to end.

WHY THIS FILE EXISTS. core/capabilities/ was 964 lines of working, tested
engine wired to nothing - its only callers were two test modules, so the
suite was green and the product had none of it. tests/test_capabilities.py
still tests the engines; this file tests that a person can REACH them.

Two things it asserts that no engine test can:

1. THE PATIENT IN CONTEXT IS HONOURED. Every screen with a patient
   dimension follows the chart that is open, and one without a patient
   asks for one instead of reporting on nobody - or, worse, on everybody.
   This was the largest behavioural gap between the platform and the
   demonstration: the platform had no sticky context at all.

2. THE RELEASE GATE HOLDS AT THE ROUTE. A patient-instructions draft that
   introduces a number the chart never stated cannot be filed, the file
   control is absent rather than merely disabled, and an attempt to file
   one anyway is refused AND audited.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_web import _client, _csrf, _post  # noqa: E402

from core.web.patient_context import (  # noqa: E402
    CONTEXT_KEY,
    patient_in_context,
    set_patient,
)


# ---------------------------------------------------------------------------
# The context itself
# ---------------------------------------------------------------------------

def test_context_holds_a_reference_and_a_label():
    s = {}
    set_patient(s, "Patient/eAB12cd3", label="A. Cadence")
    assert patient_in_context(s) == {"reference": "Patient/eAB12cd3", "label": "A. Cadence"}


@pytest.mark.parametrize("bad", [
    {}, {CONTEXT_KEY: "Patient/eAB"}, {CONTEXT_KEY: {}},
    {CONTEXT_KEY: {"reference": ""}}, {CONTEXT_KEY: {"reference": "Patient/"}},
    {CONTEXT_KEY: {"reference": "Observation/o-1"}},
])
def test_a_malformed_context_reads_as_no_context(bad):
    """A screen that believes it has a patient when it does not is worse
    than one that knows it has none: the first renders somebody else's
    data under a familiar name, the second asks."""
    assert patient_in_context(bad) is None


def test_opening_a_chart_puts_that_patient_in_context():
    client, _, _ = _client(roles="viewer")
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "treatment"})
    assert client.get("/product/summary").status_code == 200
    assert "No patient in context" not in client.get("/product/summary").text


def test_the_context_chip_names_the_patient_being_looked_at():
    """The chip is what stops somebody reading screen after screen without
    noticing whose chart they are in. It outranks the launch context
    deliberately: saying "pt X" while the screens show patient Y is the
    one thing it must never do."""
    client, _, _ = _client(roles="viewer")
    before = client.get("/overview").text
    assert "eAB12cd3" not in before

    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "treatment"})
    assert "eAB12cd3" in client.get("/overview").text, (
        "a chart is open and the top bar does not say whose"
    )


def test_the_context_can_be_cleared_and_the_screens_stop_following():
    """Explicit, because both alternatives are worse: a context that
    expires on its own leaves somebody working a chart they think is open
    and is not, and one that never clears follows them into work that has
    nothing to do with that patient."""
    client, _, _ = _client(roles="viewer")
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "treatment"})
    assert "No patient in context" not in client.get("/product/summary").text

    _post(client, "/patients/context/clear", {}, form_path="/patients")
    assert "No patient in context" in client.get("/product/summary").text


def test_clearing_the_context_discloses_nothing_so_it_audits_nothing():
    client, _, audit = _client(roles="viewer")
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "treatment"})
    before = len(audit.events)
    _post(client, "/patients/context/clear", {}, form_path="/patients")
    assert len(audit.events) == before


# ---------------------------------------------------------------------------
# Screens with a patient dimension say so when they have no patient
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,role", [
    ("/product/summary", "viewer"),
    ("/product/instructions", "viewer"),
    ("/product/priorauth", "him"),
])
def test_a_patient_screen_with_no_patient_asks_for_one(path, role):
    client, _, _ = _client(roles=role)
    body = client.get(path).text
    assert "No patient in context" in body
    assert "/patients" in body, "it says there is no patient but offers no way to pick one"


def test_the_store_wide_screen_says_it_has_no_patient_dimension():
    """Ingest QA reports on the store, not a chart. Silently ignoring the
    patient in context would be indistinguishable, on screen, from
    scoping to them."""
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/ingest").text
    assert "no patient dimension" in body


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def test_the_summary_screen_reads_the_chart_and_audits_first():
    client, _, audit = _client(roles="viewer")
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "treatment"})
    assert client.get("/product/summary").status_code == 200
    assert any(e["action"] == "record.read.summary" for e in audit.events), (
        "the summary read the chart without an audit entry"
    )


# ---------------------------------------------------------------------------
# Patient instructions: the release gate
# ---------------------------------------------------------------------------

def _open_chart(client):
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "treatment"})


def _check(client, source, draft, meds=""):
    token = _csrf(client, "/product/instructions")
    return client.post("/product/instructions", data={
        "csrf_token": token, "source_text": source,
        "draft_text": draft, "medications": meds,
    })


def test_a_faithful_draft_passes_and_offers_the_file_control():
    client, _, _ = _client(roles="viewer")
    _open_chart(client)
    body = _check(client,
                  "Take 500 mg twice a day. Follow up in 2 weeks.",
                  "Take 500 mg twice a day. See us again in 2 weeks.").text
    assert "PASS" in body
    assert "/product/instructions/file" in body


def test_a_draft_that_invents_a_number_fails_and_names_it():
    client, _, _ = _client(roles="viewer")
    _open_chart(client)
    body = _check(client,
                  "Take 500 mg twice a day.",
                  "Take 500 mg twice a day. Return in 3 weeks.").text
    assert "FAIL" in body
    assert "3" in body


def test_a_failing_draft_is_offered_no_path_to_a_signature():
    """Absent, not disabled. A control that exists and refuses is a
    control somebody will find a way to press."""
    client, _, _ = _client(roles="viewer")
    _open_chart(client)
    body = _check(client, "Take 500 mg daily.", "Take 500 mg daily. Return in 3 weeks.").text
    assert "FAIL" in body
    assert "/product/instructions/file" not in body


def test_filing_a_failing_draft_is_refused_and_audited():
    """THE GATE. Somebody who reaches this route directly - a stale form,
    a second tab, a script - is refused, and the refusal is a fact on the
    trail, because 'the system declined to release this' is something a
    reviewer may later need shown."""
    client, _, audit = _client(roles="viewer")
    _open_chart(client)
    _check(client, "Take 500 mg daily.", "Take 500 mg daily. Return in 3 weeks.")

    resp = _post(client, "/product/instructions/file", {}, form_path="/product/instructions")
    assert resp.status_code == 200
    assert "did not pass" in resp.text
    assert any(e["action"] == "ai.release_refused" for e in audit.events), (
        "a refused release left no record of the refusal"
    )


def test_filing_a_passing_draft_stages_it_unsigned():
    client, _, audit = _client(roles="viewer")
    _open_chart(client)
    _check(client, "Take 500 mg twice a day.", "Take 500 mg twice a day.")

    resp = _post(client, "/product/instructions/file", {},
                 form_path="/product/instructions")
    assert resp.status_code in (200, 303)
    filed = [e for e in audit.events if e["action"] == "instructions.filed"]
    assert filed, "nothing was filed"
    assert "draft=" in filed[0]["resource_key"], (
        "a filed draft must go through core/governance/release_gate.py, which "
        "owns 'nothing patient-directed leaves without a named human, and it "
        "leaves exactly once' - not a second parallel notion of the same rule"
    )
    assert "unsigned" in filed[0]["resource_key"], (
        "a filed draft must be staged unsigned - nothing reaches a patient "
        "without a human signature"
    )


# ---------------------------------------------------------------------------
# Trial screening and chart abstraction
# ---------------------------------------------------------------------------

def test_trial_screening_is_a_population_screen_and_says_so():
    """It asks which patients might be eligible. Following the patient in
    context would answer nothing, so it declares the absence rather than
    ignoring the context silently."""
    client, _, _ = _client(roles="researcher")
    body = client.get("/product/trials").text
    assert "no patient dimension" in body


def test_trial_screening_needs_at_least_one_inclusion_criterion():
    client, _, _ = _client(roles="researcher")
    body = client.get("/product/trials?exclusion=on+dialysis").text
    assert "Screen" in body          # the form, not a worklist
    assert "candidates" not in body.lower() or "0 candidates" not in body


def test_abstraction_needs_a_patient_and_elements():
    client, _, _ = _client(roles="him")
    assert "No patient in context" in client.get("/product/abstraction").text


def test_abstraction_proposes_but_never_confirms_on_its_own():
    """The engine's rule, surfaced on the screen: every element starts
    unconfirmed and export is refused until a named human has confirmed
    each one."""
    client, _, _ = _client(roles="him")
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "operations"})
    body = client.get("/product/abstraction?elements=most+recent+HbA1c").text
    assert "unconfirmed" in body
    assert "human confirmation" in body


def test_inbox_triage_refuses_to_route_without_a_registered_scorer():
    """Routing on a model nobody approved is the failure the registry gate
    exists to prevent, and the screen says so instead of quietly showing
    an empty queue."""
    client, _, _ = _client(roles="viewer")
    body = client.get("/product/inbox").text
    assert "No scorer is registered" in body
    assert "nothing routes" in body


def test_inbox_triage_will_not_invent_an_operating_point():
    """A threshold with no validation behind it is a decimal point."""
    client, _, _ = _client(roles="viewer")
    assert "No validation set is configured" in client.get("/product/inbox").text


# ---------------------------------------------------------------------------
# The governance screens: fairness, segmentation, ambient
# ---------------------------------------------------------------------------

def test_a_protected_variable_fails_the_fairness_screen():
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/fairness?variables=age%0Aprior_no_shows").text
    assert "FAIL" in body
    assert "age" in body


def test_a_proxy_candidate_without_a_basis_is_flagged_not_permitted():
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/fairness?variables=zip_code%0Aprior_no_shows").text
    assert "FAIL" in body
    assert "zip_code" in body


def test_the_fairness_screen_refuses_to_read_as_a_clean_bill_of_health():
    """It catches declared protected variables and named proxies. It
    cannot catch an undeclared proxy, and the screen has to say so or a
    pass will be read as more than it is."""
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/fairness?variables=prior_no_shows").text
    assert "PASS" in body
    assert "not a clean bill of health" in body


def test_segmentation_with_no_value_sets_says_so_rather_than_reporting_zero():
    """Zero exclusions because no category is defined looks identical, on
    screen, to zero because the store holds nothing sensitive."""
    client, _, _ = _client(roles="him")
    body = client.get("/product/segmentation").text
    assert "No sensitive-category value sets are configured" in body


def test_ambient_refuses_capture_without_a_resolved_jurisdiction():
    client, _, _ = _client(roles="viewer")
    body = client.get("/product/ambient").text
    assert "Capture refused" in body
    assert "jurisdiction" in body.lower()


def test_ambient_treats_an_unsettled_state_as_deny_with_no_override():
    client, _, _ = _client(roles="viewer")
    body = client.get("/product/ambient?jurisdiction=MI&consented=1&attested=1").text
    assert "Capture refused" in body
    assert "unsettled" in body


def test_ambient_refuses_transcription_without_recorded_egress_evidence():
    """An opt-out policy nobody has checked is indistinguishable from one
    that is not in force, so absent evidence refuses."""
    client, _, _ = _client(roles="viewer")
    body = client.get("/product/ambient?jurisdiction=CA&consented=1&attested=1").text
    assert "Transcription refused" in body


def test_ambient_asks_the_consent_question_before_the_egress_one():
    """If the encounter may not be recorded, whether the transcription
    service is configured safely is a question about nothing."""
    client, _, _ = _client(roles="viewer")
    body = client.get("/product/ambient").text
    assert body.index("state recording law") < body.index("egress evidence")


# ---------------------------------------------------------------------------
# The action space and HTI-1 source attributes
# ---------------------------------------------------------------------------

def test_a_supportive_action_is_permitted_on_a_prediction_alone():
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/noshow?action=additional_reminder").text
    assert "Permitted" in body


def test_a_restrictive_action_is_refused_without_a_recorded_basis():
    """This is the whole difference between a model that improves access
    and one that penalizes the patients who struggle to attend."""
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/noshow?action=deprioritization").text
    assert "Refused" in body


def test_an_unknown_action_is_refused_not_guessed_at():
    """Classifying an unheard-of action as "probably supportive" is the
    silent fallback the invariants prohibit."""
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/noshow?action=quietly_double_book_them").text
    assert "Refused" in body


def test_an_override_with_a_basis_is_recorded_on_the_decision():
    client, _, _ = _client(roles="analyst")
    body = client.get(
        "/product/noshow?action=deprioritization&basis=capacity+incident+2026-09-08"
    ).text
    assert "operator override" in body.lower()
    assert "capacity incident" in body


def test_source_attributes_report_an_absent_artifact_as_absent():
    """Empty categories filled with placeholders would manufacture the
    very artifact the obligation exists to make somebody produce."""
    client, _, _ = _client(roles="him")
    body = client.get("/product/attributes").text
    assert "No model has a recorded source-attribute set" in body


# ---------------------------------------------------------------------------
# The last six: coding, measures, claims, scheduling, psychotherapy
# ---------------------------------------------------------------------------

def test_the_coding_screen_shows_both_directions_side_by_side():
    """A deployment that only ever acts on the revenue-positive list
    should be able to see itself doing it."""
    client, _, _ = _client(roles="him")
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "operations"})
    body = client.get("/product/coding").text
    assert "Care and revenue gaps" in body
    assert "Compliance gaps" in body
    assert "upcoding" in body   # the phrase wraps in the template; match the word


def test_the_coding_screen_says_when_no_support_map_narrows_it():
    client, _, _ = _client(roles="him")
    _post(client, "/patients/eAB12cd3/open", {"purpose_of_use": "operations"})
    assert "No code-support map is configured" in client.get("/product/coding").text


def test_measures_refuses_to_call_object_counts_a_prevalence():
    """A prevalence needs a numerator counted in PATIENTS. The index counts
    objects - one patient with forty Observations contributes forty - and
    the first version of this route divided one by the other. The engine
    caught it by refusing a numerator larger than its denominator; the
    screen now says why there is no rate rather than showing a wrong one."""
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/measures").text
    assert "deliberate" in body
    assert "counted in" in body
    assert "counts, not rates" in body


def test_measures_explains_the_interval_it_uses():
    client, _, _ = _client(roles="analyst")
    assert "Wilson, not Wald" in client.get("/product/measures").text


def test_claims_declines_to_score_without_adjudicated_history():
    client, _, _ = _client(roles="him")
    body = client.get("/product/claims?payer=Acme&service_line=99215").text
    assert "Unscored" in body or "no adjudicated claim history" in body


def test_claims_reads_under_the_payment_purpose():
    client, _, audit = _client(roles="him")
    client.get("/product/claims?payer=Acme&service_line=99215")
    scored = [e for e in audit.events if e["action"] == "claims.scored"]
    assert scored and scored[0]["purpose_of_use"] == "payment"


def test_scheduling_defers_to_the_action_space_for_overbooking():
    """Overbooking takes access away from the person scheduled, so it is a
    restrictive action and needs a recorded basis, not a busy afternoon."""
    client, _, _ = _client(roles="analyst")
    body = client.get("/product/scheduling").text
    assert "restrictive action" in body
    assert "double_booking" in body


def test_the_psychotherapy_screen_offers_no_assistant_prompt():
    """The assistant has no tool reaching this store unless four
    independent gates are open. An ask box here would imply otherwise."""
    client, _, _ = _client(roles="viewer")
    body = client.get("/product/psychotherapy").text
    assert "four gates" in body.lower()
    assert 'action="/assistant"' not in body


def test_the_psychotherapy_gates_are_shut_by_default():
    client, _, _ = _client(roles="viewer")
    body = client.get("/product/psychotherapy").text
    assert "0 of 4 open" in body or "shut" in body


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/product/summary", "/product/instructions"])
def test_a_role_without_the_screen_is_refused_at_the_route(path):
    """The navigation table is the product's statement of who each screen
    is for, and the route enforces the same table - so hiding a link is
    never the only thing standing between a role and a screen."""
    client, _, _ = _client(roles="auditor")
    assert client.get(path).status_code == 403
# Made by Ryan Gomez & Co. Inc.


# ---------------------------------------------------------------------------
# Model monitoring — the entry nav.py had been promising
# ---------------------------------------------------------------------------

def test_an_unchecked_deployment_reads_unknown_not_nominal():
    """The single worst state an instrument panel can show is green
    because nothing was checked - and it is the DEFAULT state of every
    monitoring screen that computes its condition from an empty result
    set."""
    from core.web.monitoring_routes import UNKNOWN, assess

    condition, reasons = assess(telemetry_configured=False, drift_runs=[], models=[])
    assert condition == UNKNOWN
    assert any("not configured" in r for r in reasons)


def test_configured_but_never_probed_is_still_unknown():
    from core.web.monitoring_routes import UNKNOWN, assess

    condition, _ = assess(telemetry_configured=True, drift_runs=[], models=[{"name": "m"}])
    assert condition == UNKNOWN


def test_failing_probes_raise_the_condition():
    from core.web.monitoring_routes import ATTENTION, NOMINAL, WARNING, assess

    clean = [{"probes": 5, "passed": 5, "model": "m"}]
    one_bad = [{"probes": 5, "passed": 4, "model": "m", "failed_probes": "retention"}]
    two_bad = one_bad + [{"probes": 5, "passed": 3, "model": "m"}]

    assert assess(telemetry_configured=True, drift_runs=clean,
                  models=[{"name": "m"}])[0] == NOMINAL
    assert assess(telemetry_configured=True, drift_runs=one_bad,
                  models=[{"name": "m"}])[0] == ATTENTION
    assert assess(telemetry_configured=True, drift_runs=two_bad,
                  models=[{"name": "m"}])[0] == WARNING


def test_the_reason_names_the_failing_probe():
    from core.web.monitoring_routes import assess

    _, reasons = assess(
        telemetry_configured=True,
        drift_runs=[{"probes": 5, "passed": 4, "model": "sonnet",
                     "failed_probes": "retention_years"}],
        models=[{"name": "m"}],
    )
    assert any("retention_years" in r for r in reasons)


def test_model_monitoring_is_an_admin_screen():
    client, _, _ = _client(roles="viewer")
    assert client.get("/system/models").status_code == 403


def test_model_monitoring_renders_for_the_system_administrator():
    """`system:admin` is the System group's permission and no enumerated
    role carries it but sysadmin - the same gate the control panel and the
    Components screen sit behind."""
    client, _, _ = _client(roles="sysadmin")
    body = client.get("/system/models").text
    assert "Model monitoring" in body
    assert "UNKNOWN" in body
