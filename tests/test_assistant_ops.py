# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Assistant telemetry and drift probes.

Telemetry's two contracts: it records METRICS AND NEVER CONTENT (the
insert's column list is checked against a denylist of content-shaped
names), and a telemetry failure NEVER fails an answer. Drift's contract:
a probe file that would silently check nothing is refused at load, and
evaluation is pure so expectations are testable without a model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.assistant import drift, telemetry  # noqa: E402


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

class RecordingConn:
    def __init__(self, fail_on_execute=False):
        self.fail_on_execute = fail_on_execute
        self.executed = []
        self.committed = False

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, sql, params):
                if conn.fail_on_execute:
                    raise RuntimeError("disk full")
                conn.executed.append((" ".join(sql.split()), params))

            def close(self):
                pass

        return Cur()

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def test_an_interaction_row_is_metrics_only():
    conn = RecordingConn()
    ok = telemetry.record_interaction(
        lambda: conn,
        username="dr.chen", roles="researcher", page_key="patients",
        provider="bedrock", model="us.anthropic.claude-sonnet-5",
        latency_ms=1200, input_tokens=900, output_tokens=250,
        tool_calls=2, tools_used="search_documentation,search_clinical_records",
        phi_reads=1, refused=False,
    )
    assert ok and conn.committed
    sql, params = conn.executed[0]
    # The contract from telemetry_schema.sql's header: no content-shaped
    # column may ever appear in the insert.
    for forbidden in ("question", "answer", "content", "snippet", "arguments"):
        assert forbidden not in sql.lower().split("values")[0], (
            f"telemetry must never store {forbidden!r}"
        )
    assert "dr.chen" in params


def test_a_telemetry_failure_never_raises():
    assert telemetry.record_interaction(lambda: RecordingConn(fail_on_execute=True),
                                        username="u") is False
    def broken_factory():
        raise RuntimeError("no database")
    assert telemetry.record_interaction(broken_factory, username="u") is False
    assert telemetry.record_interaction(None, username="u") is False


# ---------------------------------------------------------------------------
# Drift: probe loading refuses silent no-ops
# ---------------------------------------------------------------------------

def _write_probes(tmp_path, text):
    p = tmp_path / "probes.yaml"
    p.write_text(text)
    return str(p)


def test_probes_load(tmp_path):
    probes = drift.load_probes(_write_probes(tmp_path, """
probes:
  - name: one
    question: Is retention enforced?
    expect:
      refused: false
      answer_must_contain_any: ["recorded"]
"""))
    assert len(probes) == 1
    assert probes[0].answer_must_contain_any == ("recorded",)


def test_a_misspelled_expectation_is_refused_not_defaulted(tmp_path):
    with pytest.raises(drift.ProbeConfigError) as exc:
        drift.load_probes(_write_probes(tmp_path, """
probes:
  - name: one
    question: q
    expect:
      answer_must_containe_any: ["x"]
"""))
    assert "unknown expectation" in str(exc.value)


def test_duplicate_probe_names_are_refused(tmp_path):
    with pytest.raises(drift.ProbeConfigError):
        drift.load_probes(_write_probes(tmp_path, """
probes:
  - {name: one, question: a}
  - {name: one, question: b}
"""))


def test_the_committed_example_probe_file_loads():
    probes = drift.load_probes(str(ROOT / "config/assistant_probes.example.yaml"))
    assert len(probes) >= 3
    assert any(p.expect_refused for p in probes), (
        "the suite should include a refusal probe - the PHI gate is the "
        "behaviour least affordable to lose"
    )


# ---------------------------------------------------------------------------
# Drift: evaluation is pure and literal
# ---------------------------------------------------------------------------

def _reply(**kw):
    defaults = dict(text="", sources=[], tools_used=[], refused=False,
                    input_tokens=0, output_tokens=0)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_evaluate_passes_when_expectations_hold():
    probe = drift.Probe(
        name="p", question="q",
        answer_must_contain_any=("recorded",),
        must_cite_any=("docs/",),
        must_use_tool="search_documentation",
    )
    reply = _reply(text="Retention is recorded, not enforced.",
                   sources=["docs/COMPLIANCE.md > Retention"],
                   tools_used=["search_documentation"])
    assert drift.evaluate(probe, reply).passed


def test_evaluate_names_each_failed_expectation():
    probe = drift.Probe(
        name="p", question="q", expect_refused=True,
        answer_must_not_contain=("Object Lock prevents",),
    )
    reply = _reply(text="Object Lock prevents deletion.", refused=False)
    result = drift.evaluate(probe, reply)
    assert not result.passed
    assert len(result.failures) == 2
    assert any("refused" in f for f in result.failures)
    assert any("forbidden" in f for f in result.failures)


def test_an_unexpected_refusal_fails_the_probe():
    probe = drift.Probe(name="p", question="q",
                        answer_must_contain_any=("recorded",))
    result = drift.evaluate(probe, _reply(text="I can't help with that.", refused=True))
    assert not result.passed


def test_drift_results_record_as_probe_rows():
    conn = RecordingConn()
    result = drift.ProbeResult(
        probe=drift.Probe(name="retention", question="q"),
        passed=False, failures=["cited none of ['docs/']"],
        latency_ms=800, tools_used=("search_documentation",),
    )
    drift.record_results(lambda: conn, [result], provider="bedrock", model="m1")
    sql, params = conn.executed[0]
    assert "drift_probe" in params
    assert "retention" in params
    assert False in params  # probe_passed
# Made by Ryan Gomez & Co. Inc.
