# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The remaining modules no test had ever imported.

WHY THIS FILE EXISTS. Completing the sweep: after the audit sinks, the
storage backends and the purge paths, these were the last core modules
that the whole suite never loaded - the release version, the healthcheck
reporter, the scheduler watermark, the bulk-export key parser, the audit
chain verifier, the retention rules reviewer, and the account CLI.

Most are `main()` entrypoints, and a test that runs `main()` tests
argparse. So each test below reaches for the piece of the module that
holds an actual decision - the part that would be wrong quietly - and
leaves the argument parsing alone.

That every one of these modules can now be IMPORTED at all is itself
worth something. The pre-push gate byte-compiles them, so a syntax error
was already caught; an ImportError from a bad `from x import y` was not,
and would have surfaced on an operator's host at 2am rather than here.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core  # noqa: E402
import core.assistant.__main__ as assistant_cli  # noqa: E402
import core.audit.verify as audit_verify  # noqa: E402
import core.config.retention_rules_check as rules_check  # noqa: E402
import core.dicom.__main__ as dicom_cli  # noqa: E402
import core.fhir.bulk_scheduler as bulk_scheduler  # noqa: E402
import core.fhir.scheduler as scheduler  # noqa: E402
import core.web.useradmin as useradmin  # noqa: E402
from core.fhir.bulk_export import _resource_type_from_key  # noqa: E402
from core.healthcheck import FAIL, PASS, WARN, Check  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# core/__init__.py - the release version
# ---------------------------------------------------------------------------

def test_the_version_is_the_release_file_not_the_literal():
    """RELEASE is the one release source and core/__init__ reads it. The
    module-level literal is only a fallback for a tree without the file -
    if the two disagree, everything that reports a version (the web
    footer, the Components screen, the image label) reports the stale one.
    """
    assert core.__version__ == (ROOT / "RELEASE").read_text(encoding="utf-8").strip()


def test_the_version_falls_back_rather_than_crashing_without_a_release_file(monkeypatch):
    assert core._release("9.9.9-fallback")  # the real file is present here
    monkeypatch.setattr(core._Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError))
    assert core._release("9.9.9-fallback") == "9.9.9-fallback"


# ---------------------------------------------------------------------------
# core/healthcheck.py - the report and its exit code
# ---------------------------------------------------------------------------

def test_the_healthcheck_exit_code_is_driven_by_failures_only(capsys):
    """A WARN must not fail the process. This exit code is what a
    deployment's readiness probe reads, so a warning that exits non-zero
    takes a healthy deployment out of rotation."""
    check = Check()
    check.add(PASS, "config.load", "provider=aws")
    check.add(WARN, "assistant.enabled", "optional add-on not configured")
    assert check.report() == 0

    check.add(FAIL, "storage.reachable", "bucket not found")
    assert check.report() == 1
    assert "1 failed" in capsys.readouterr().out


def test_the_healthcheck_report_names_every_check_it_ran(capsys):
    check = Check()
    check.add(PASS, "config.load")
    check.add(FAIL, "db.connect", "connection refused")
    check.report()
    out = capsys.readouterr().out
    assert "config.load" in out and "db.connect" in out and "connection refused" in out


# ---------------------------------------------------------------------------
# core/fhir/scheduler.py - the watermark
# ---------------------------------------------------------------------------

class _FakeIndex:
    def __init__(self, state=None):
        self.state = state or {}
        self.writes: list[tuple] = []

    def read_index_state(self, conn, key):
        return self.state.get(key)

    def write_index_state(self, conn, key, value):
        self.writes.append((key, value))
        self.state[key] = value


def test_no_watermark_means_a_full_run_not_an_error(monkeypatch):
    """Both "first ever run" and "no Postgres configured" are normal and
    mean the same thing: process everything. Ingestion is idempotent, so
    a full run is safe; raising here would take the scheduler down on a
    deployment that simply has no index."""
    monkeypatch.setattr(scheduler, "db_index", _FakeIndex())
    assert scheduler.load_watermark(None) is None
    assert scheduler.load_watermark(object()) is None


def test_a_watermark_round_trips_through_the_index(monkeypatch):
    idx = _FakeIndex()
    monkeypatch.setattr(scheduler, "db_index", idx)
    ts = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    scheduler.save_watermark(object(), ts)
    assert idx.writes == [(scheduler.SCHEDULER_WATERMARK_KEY, ts.isoformat())]
    assert scheduler.load_watermark(object()) == ts


def test_saving_without_an_index_is_a_no_op_rather_than_a_crash(monkeypatch):
    idx = _FakeIndex()
    monkeypatch.setattr(scheduler, "db_index", idx)
    scheduler.save_watermark(None, datetime.now(timezone.utc))
    assert idx.writes == []


# ---------------------------------------------------------------------------
# core/fhir/bulk_export.py - the key parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("fhir/Observation/obs-1.json", "Observation"),
    ("fhir/Patient/eXYZ.json", "Patient"),
    ("fhir/DocumentReference/dr-1.json", "DocumentReference"),
    ("audit/2026/09/08/x.json", "Unknown"),
    ("fhir/Patient", "Unknown"),
    ("", "Unknown"),
])
def test_the_resource_type_is_read_off_the_key_without_decrypting(key, expected):
    """This runs BEFORE decryption so progress output and per-type NDJSON
    selection can start early. A key it cannot parse must sort to Unknown,
    never guess - an export that files an Observation under Patient is a
    disclosure of the wrong resource type."""
    assert _resource_type_from_key(key) == expected


# ---------------------------------------------------------------------------
# The remaining entrypoints: importable, and wired to the shared factory
# ---------------------------------------------------------------------------

def test_the_audit_verifier_goes_through_the_storage_factory():
    """FOUND AND FIXED in the 2026-08-17 audit: this tool hardcoded
    S3AuditSink and exited 2 on GCP and Azure - nightly chain
    verification, the control it exists to automate, was impossible on two
    of three supported clouds. It routes through build_audit_sink() now,
    and this is the assertion that keeps it there."""
    src = (ROOT / "core/audit/verify.py").read_text(encoding="utf-8")
    assert "build_audit_sink" in src
    assert "S3AuditSink(" not in src, "the AWS-only construction is back"


@pytest.mark.parametrize("module", [
    assistant_cli, audit_verify, dicom_cli, rules_check, useradmin, bulk_scheduler,
])
def test_every_entrypoint_module_exposes_a_main(module):
    """Each is run as `python -m <module>`. An entrypoint that imports but
    has no main() fails on the operator's host, not here."""
    assert callable(getattr(module, "main", None)), module.__name__


def test_the_account_cli_records_an_actor_distinguishable_from_the_interface():
    """Every operation here IS audited - a command line path that skipped
    the audit would be an unaudited way to grant access to PHI. The actor
    is prefixed so an entry made on the host is not mistaken for one made
    by somebody signed in."""
    src = (ROOT / "core/web/useradmin.py").read_text(encoding="utf-8")
    assert '"cli:' in src or "'cli:" in src, (
        "the CLI actor prefix is gone; a host-side grant would now be "
        "indistinguishable from one made in the interface"
    )
# Made by Ryan Gomez & Co. Inc.
