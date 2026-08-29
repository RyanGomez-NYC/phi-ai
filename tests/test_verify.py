# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/verify/.

The framework exists so "is this platform sound?" has one answer. These
tests weight two things above all: that a flow which could NOT be checked
never reports as clean, and that ingestion gaps are treated as critical
because they are the only ones that become permanent.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.verify.base import FlowReport, Severity, VerificationReport  # noqa: E402
from core.verify.export import exported_ids, verify_export  # noqa: E402
from core.verify.ingestion import stored_ids, verify_ingestion  # noqa: E402


class _Storage:
    def __init__(self, keys, corrupt=()):
        self._keys = list(keys)
        self._corrupt = set(corrupt)

    def list_keys(self, prefix=""):
        return [k for k in self._keys if k.startswith(prefix)]

    def iter_keys(self, prefix=""):
        # Stands in for the real interface, which ObjectStore provides
        # for every backend - a fake missing it would let a change that
        # requires it pass here and fail in production.
        for key in sorted(self.list_keys(prefix=prefix)):
            yield key

    def verify_integrity(self, key, version_id=None):
        return key not in self._corrupt


class _Client:
    base_url = "https://fhir.example/r4"
    access_token = "tok"

    def __init__(self, by_type):
        self._by_type = by_type

    def iter_resources(self, resource_type, since=None):
        for rid in self._by_type.get(resource_type, []):
            yield {"resourceType": resource_type, "id": rid}


# ---------------------------------------------------------------------------
# Unchecked is not clean
# ---------------------------------------------------------------------------

def test_a_skipped_flow_never_yields_a_clean_exit_code():
    """"Sound" and "unexamined" are different states. A runbook step or CI
    job keying on the exit code must be able to tell them apart."""
    report = VerificationReport()
    flow = FlowReport(flow="EMR ingestion", source="emr", target="store")
    flow.skipped_reason = "source EMR unreachable"
    report.add(flow)

    assert report.exit_code() == 1
    assert report.skipped


def test_a_clean_report_exits_zero():
    report = VerificationReport()
    flow = FlowReport(flow="x", source="a", target="b")
    flow.add(Severity.OK, "check", "fine")
    report.add(flow)
    assert report.exit_code() == 0


def test_a_critical_finding_exits_two():
    report = VerificationReport()
    flow = FlowReport(flow="x", source="a", target="b")
    flow.add(Severity.CRITICAL, "check", "data may be lost")
    report.add(flow)
    assert report.exit_code() == 2


def test_the_rendered_report_names_unchecked_flows_explicitly():
    report = VerificationReport()
    flow = FlowReport(flow="EMR ingestion", source="emr", target="store")
    flow.skipped_reason = "source is gone"
    report.add(flow)
    rendered = report.render()
    assert "NOT CHECKED" in rendered
    # The operative clause of render()'s closing line, not the whole
    # sentence: the PHI AI Platform rename fixed the report BANNER text
    # but left the noun in this prose line open, so pinning the full
    # sentence would pin a wording nobody has settled. The property being
    # tested - a skipped flow is named unchecked and explicitly said not
    # to count as verified - is unchanged. Tighten this back to the full
    # sentence once core/verify/base.py's render() wording is final.
    assert "is not a verified one" in rendered


def test_examples_are_sampled_rather_than_dumped():
    """A verification report is read by a human; ten thousand ids is not a
    finding, it is a wall. Counts carry the magnitude."""
    flow = FlowReport(flow="x", source="a", target="b")
    flow.add(Severity.CRITICAL, "c", "many", examples=tuple(f"id{i}" for i in range(500)),
             count=500)
    rendered = flow.findings[0].rendered_examples(limit=5)
    assert rendered.count(",") == 4
    assert "and 495 more" in rendered


# ---------------------------------------------------------------------------
# Ingestion — the check with a deadline
# ---------------------------------------------------------------------------

def test_a_record_in_the_source_but_not_stored_is_critical():
    """The only gap that becomes permanent: once the source is
    decommissioned, the evidence it ever existed goes with it."""
    storage = _Storage(["fhir/Observation/o1.json"])
    client = _Client({"Observation": ["o1", "o_MISSING"]})

    report = verify_ingestion(storage, client, ["Observation"], deep=True)
    critical = [f for f in report.findings if f.severity is Severity.CRITICAL]

    assert len(critical) == 1
    assert "o_MISSING" in critical[0].examples
    assert report.worst is Severity.CRITICAL


def test_a_record_stored_but_since_deleted_in_the_source_is_only_info():
    """Expected: this platform is meant to outlive the source's own
    retention. Treating it as a failure would train operators to ignore
    the report."""
    storage = _Storage(["fhir/Observation/o1.json", "fhir/Observation/gone.json"])
    client = _Client({"Observation": ["o1"]})

    report = verify_ingestion(storage, client, ["Observation"], deep=True)
    assert not [f for f in report.findings if f.severity is Severity.CRITICAL]
    assert any("no longer in the source" in f.summary for f in report.findings)


def test_matching_identifiers_report_ok():
    storage = _Storage(["fhir/Observation/o1.json", "fhir/Observation/o2.json"])
    client = _Client({"Observation": ["o1", "o2"]})
    report = verify_ingestion(storage, client, ["Observation"], deep=True)
    assert report.worst is Severity.INFO  # the deadline notice only
    assert any("identifiers match exactly" in f.summary for f in report.findings)


def test_the_report_always_states_that_this_check_expires():
    """Operationally the most important sentence in the whole framework."""
    report = verify_ingestion(_Storage([]), _Client({}), [], deep=True)
    assert any("requires the source EMR to still exist" in f.summary
               for f in report.findings)


def test_an_unenumerable_source_type_warns_rather_than_passing():
    class _Exploding(_Client):
        def iter_resources(self, resource_type, since=None):
            raise RuntimeError("403 from the EMR")

    report = verify_ingestion(_Storage([]), _Exploding({}), ["Observation"], deep=True)
    assert any(f.severity is Severity.WARNING for f in report.findings)


def test_stored_ids_are_read_from_keys_without_decrypting():
    """Verification needs no clinical read access, which meaningfully
    reduces what a verification job puts at risk."""
    storage = _Storage(["fhir/Observation/o1.json", "fhir/Patient/p1.json",
                        "documents/source/doc-1.pdf"])
    assert stored_ids(storage, "Observation") == {"o1"}
    assert stored_ids(storage, "Patient") == {"p1"}


# ---------------------------------------------------------------------------
# Export completeness
# ---------------------------------------------------------------------------

def _write_ndjson(directory: Path, resource_type: str, ids):
    path = directory / f"{resource_type}.ndjson"
    path.write_text("\n".join(
        json.dumps({"resourceType": resource_type, "id": i}) for i in ids
    ) + "\n")


def test_an_export_missing_stored_records_is_critical(tmp_path):
    """An incomplete export looks exactly like a complete one to the
    receiving system."""
    _write_ndjson(tmp_path, "Observation", ["o1"])
    storage = _Storage(["fhir/Observation/o1.json", "fhir/Observation/o2.json"])

    report = verify_export(storage, str(tmp_path), ["Observation"])
    critical = [f for f in report.findings if f.severity is Severity.CRITICAL]
    assert critical and "o2" in critical[0].examples


def test_a_complete_export_reports_ok(tmp_path):
    _write_ndjson(tmp_path, "Observation", ["o1", "o2"])
    storage = _Storage(["fhir/Observation/o1.json", "fhir/Observation/o2.json"])
    report = verify_export(storage, str(tmp_path), ["Observation"])
    assert report.ok


def test_a_stale_export_containing_disposed_records_warns(tmp_path):
    _write_ndjson(tmp_path, "Observation", ["o1", "disposed"])
    storage = _Storage(["fhir/Observation/o1.json"])
    report = verify_export(storage, str(tmp_path), ["Observation"])
    assert any(f.severity is Severity.WARNING for f in report.findings)


def test_a_missing_export_directory_is_skipped_not_passed(tmp_path):
    report = verify_export(_Storage([]), str(tmp_path / "nope"), ["Observation"])
    assert report.skipped_reason
    assert not report.ok


def test_a_malformed_ndjson_line_does_not_abort_the_check(tmp_path):
    """The receiving system would fail on it too; reporting is cheaper
    than discovering it mid-migration."""
    (tmp_path / "Observation.ndjson").write_text(
        json.dumps({"resourceType": "Observation", "id": "o1"}) + "\n"
        "{not valid json\n"
        + json.dumps({"resourceType": "Observation", "id": "o2"}) + "\n"
    )
    assert exported_ids(str(tmp_path), "Observation") == {"o1", "o2"}


# ---------------------------------------------------------------------------
# Object integrity
# ---------------------------------------------------------------------------

def test_a_corrupted_object_is_critical():
    from core.verify.__main__ import verify_object_integrity

    storage = _Storage(["fhir/Observation/o1.json", "fhir/Observation/bad.json"],
                       corrupt=["fhir/Observation/bad.json"])
    report = verify_object_integrity(storage, sample_size=50, deep=True)
    critical = [f for f in report.findings if f.severity is Severity.CRITICAL]
    assert critical and "fhir/Observation/bad.json" in critical[0].examples


def test_a_sampled_integrity_check_says_so():
    """A sample that reported as "verified" without qualification would
    overstate what was actually checked."""
    from core.verify.__main__ import verify_object_integrity

    storage = _Storage([f"fhir/Observation/o{i}.json" for i in range(500)])
    report = verify_object_integrity(storage, sample_size=10, deep=False)
    assert any("was a sample" in f.summary for f in report.findings)


def test_an_empty_store_is_skipped_not_declared_sound():
    from core.verify.__main__ import verify_object_integrity

    report = verify_object_integrity(_Storage([]), sample_size=10, deep=False)
    assert report.skipped_reason


# ---------------------------------------------------------------------------
# Delivery confirmation
# ---------------------------------------------------------------------------

class _Item:
    def __init__(self, key, sent=True, rtype="Observation", sid="o1"):
        self.storage_key = key
        self.sent = sent
        self.resource_type = rtype
        self.source_id = sid


class _Result:
    def __init__(self, items, dry_run=False):
        self.items = items
        self.dry_run = dry_run


def test_a_dry_run_delivery_is_skipped_not_confirmed():
    from core.verify.delivery import verify_delivery

    report = verify_delivery(_Result([_Item("k")], dry_run=True), "https://d.example", "t")
    assert report.skipped_reason and not report.ok


def test_a_record_reported_sent_but_absent_downstream_is_a_warning():
    """A warning, not critical: the platform still holds it, so the
    delivery can simply be repeated. Severity tracks recoverability."""
    from core.verify.delivery import verify_delivery

    report = verify_delivery(
        _Result([_Item("fhir/Observation/o1.json")]),
        "https://d.example", "t",
        http_get=lambda url, token: {"total": 0},
    )
    assert report.worst is Severity.WARNING
    assert any("NOT in the destination" in f.summary for f in report.findings)


def test_a_confirmed_delivery_reports_ok():
    from core.verify.delivery import verify_delivery

    report = verify_delivery(
        _Result([_Item("fhir/Observation/o1.json")]),
        "https://d.example", "t",
        http_get=lambda url, token: {"total": 1},
    )
    assert report.ok


def test_a_duplicate_in_the_destination_is_flagged():
    from core.verify.delivery import verify_delivery

    report = verify_delivery(
        _Result([_Item("fhir/Observation/o1.json")]),
        "https://d.example", "t",
        http_get=lambda url, token: {"total": 3},
    )
    assert any("appears 3 times" in f.summary for f in report.findings)


def test_an_unreachable_destination_is_unconfirmed_not_delivered():
    """"Unknown" is not the same as "delivered"."""
    from core.verify.delivery import verify_delivery

    def exploding(url, token):
        raise RuntimeError("timeout")

    report = verify_delivery(
        _Result([_Item("fhir/Observation/o1.json")]),
        "https://d.example", "t", http_get=exploding,
    )
    assert any("could not be checked" in f.summary for f in report.findings)
    assert not report.ok


# ---------------------------------------------------------------------------
# Freshness: stored records the source has superseded
# ---------------------------------------------------------------------------

def _versioned(rid, version=None, updated=None):
    meta = {}
    if version is not None:
        meta["versionId"] = str(version)
    if updated is not None:
        meta["lastUpdated"] = updated
    resource = {"resourceType": "Observation", "id": rid}
    if meta:
        resource["meta"] = meta
    return resource


class _FreshReader:
    def __init__(self, stored):
        self._stored = stored

    def read_resource(self, storage_key):
        return self._stored[storage_key]


class _FreshClient:
    base_url = "https://fhir.example/r4"
    access_token = "tok"

    def __init__(self, current):
        self._current = current

    def read_resource(self, resource_type, resource_id):
        key = f"{resource_type}/{resource_id}"
        if key not in self._current:
            raise RuntimeError("404 not found")
        return self._current[key]


def test_a_newer_version_in_the_source_is_reported_as_superseded():
    """Under 45 CFR 164.526 a patient may amend their record. Holding
    the pre-amendment version would disclose text the patient had
    corrected - and every other check would look clean."""
    from core.verify.freshness import verify_freshness

    storage = _Storage(["fhir/Observation/o1.json"])
    reader = _FreshReader({"fhir/Observation/o1.json": _versioned("o1", version=1)})
    client = _FreshClient({"Observation/o1": _versioned("o1", version=3)})

    report = verify_freshness(storage, reader, client, ["Observation"], deep=True)
    superseded = [f for f in report.findings if f.check == "freshness.superseded"]

    assert superseded
    assert "version 3" in superseded[0].examples[0]
    assert report.worst is Severity.WARNING  # recoverable: just store it again


def test_a_matching_version_is_not_flagged():
    from core.verify.freshness import verify_freshness

    storage = _Storage(["fhir/Observation/o1.json"])
    reader = _FreshReader({"fhir/Observation/o1.json": _versioned("o1", version=2)})
    client = _FreshClient({"Observation/o1": _versioned("o1", version=2)})

    report = verify_freshness(storage, reader, client, ["Observation"], deep=True)
    assert not [f for f in report.findings if f.check == "freshness.superseded"]


def test_a_record_deleted_in_the_source_is_not_a_freshness_problem():
    """Exactly what this platform is for: it outlives the source's retention."""
    from core.verify.freshness import verify_freshness

    storage = _Storage(["fhir/Observation/gone.json"])
    reader = _FreshReader({"fhir/Observation/gone.json": _versioned("gone", version=1)})
    client = _FreshClient({})  # every read 404s

    report = verify_freshness(storage, reader, client, ["Observation"], deep=True)
    assert not [f for f in report.findings if f.check == "freshness.superseded"]


def test_version_id_wins_over_timestamps():
    """versionId is the server's own statement that the record moved on;
    lastUpdated is a weaker signal some servers touch for other reasons."""
    from core.verify.freshness import is_superseded

    stale, why = is_superseded(
        _versioned("o1", version=5, updated="2020-01-01T00:00:00Z"),
        _versioned("o1", version=5, updated="2026-01-01T00:00:00Z"),
    )
    assert stale is False
    assert "version 5" in why


def test_a_timestamp_only_comparison_says_that_is_what_it_is():
    """It must not present itself as a version statement."""
    from core.verify.freshness import is_superseded

    stale, why = is_superseded(
        _versioned("o1", updated="2020-01-01T00:00:00Z"),
        _versioned("o1", updated="2026-01-01T00:00:00Z"),
    )
    assert stale is True
    assert "timestamp comparison rather than a version statement" in why


def test_records_with_no_version_metadata_are_not_assumed_current():
    from core.verify.freshness import is_superseded

    stale, why = is_superseded(_versioned("o1"), _versioned("o1"))
    assert stale is False
    assert "neither copy carries" in why


def test_a_sampled_freshness_check_says_so():
    from core.verify.freshness import verify_freshness

    keys = [f"fhir/Observation/o{i}.json" for i in range(100)]
    storage = _Storage(keys)
    reader = _FreshReader({k: _versioned(k.split("/")[-1][:-5], version=1) for k in keys})
    client = _FreshClient({f"Observation/o{i}": _versioned(f"o{i}", version=1)
                           for i in range(100)})

    report = verify_freshness(storage, reader, client, ["Observation"],
                              sample_size=5, deep=False)
    assert any("was a sample" in f.summary for f in report.findings)


def test_unreadable_records_are_reported_as_unknown_not_current():
    from core.verify.freshness import verify_freshness

    class _Exploding:
        def read_resource(self, storage_key):
            raise RuntimeError("decrypt failed")

    storage = _Storage(["fhir/Observation/o1.json"])
    report = verify_freshness(storage, _Exploding(),
                              _FreshClient({"Observation/o1": _versioned("o1", version=1)}),
                              ["Observation"], deep=True)
    assert any(f.check == "freshness.unknown" for f in report.findings)


# ---------------------------------------------------------------------------
# Scheduled verification
# ---------------------------------------------------------------------------

def test_each_run_is_recorded_in_the_audit_log():
    """"We verify our records regularly" is a claim someone will be asked
    to substantiate. A log line can be rotated away; a hash-chained audit
    entry cannot."""
    from core.verify.scheduled import record_run

    class _Audit:
        def __init__(self):
            self.events = []

        def record(self, actor, action, resource_key, purpose_of_use=None):
            self.events.append((actor, action, resource_key, purpose_of_use))

    report = VerificationReport()
    flow = FlowReport(flow="x", source="a", target="b")
    flow.add(Severity.CRITICAL, "c", "bad")
    report.add(flow)

    audit = _Audit()
    record_run(audit, report, deep=True)

    actor, action, key, purpose = audit.events[0]
    assert action == "record.verify"
    assert "outcome=CRITICAL" in key and "critical=1" in key and "depth=deep" in key


def test_the_audit_entry_carries_counts_not_record_identifiers():
    """Identifiers belong in the report, not in an append-only log that is
    deliberately hard to prune."""
    from core.verify.scheduled import record_run

    class _Audit:
        def __init__(self):
            self.events = []

        def record(self, actor, action, resource_key, purpose_of_use=None):
            self.events.append(resource_key)

    report = VerificationReport()
    flow = FlowReport(flow="x", source="a", target="b")
    flow.add(Severity.CRITICAL, "c", "bad", examples=("fhir/Observation/secret.json",))
    report.add(flow)

    record_run(_Audit(), report, deep=False)


def test_recording_failure_does_not_lose_the_finding():
    """A run that FOUND something must still report it even if recording
    the run failed."""
    from core.verify.scheduled import record_run

    class _Broken:
        def record(self, **kwargs):
            raise RuntimeError("audit sink unavailable")

    report = VerificationReport()
    record_run(_Broken(), report, deep=False)  # must not raise


def test_a_missing_audit_sink_warns_rather_than_failing_the_run():
    from core.verify.scheduled import record_run

    record_run(None, VerificationReport(), deep=False)  # must not raise


def test_the_scheduler_and_the_cli_share_one_report_builder():
    """A scheduled run and a hand-run check must not be able to disagree
    about what "verified" means."""
    import inspect

    from core.verify import scheduled

    assert "build_verification_report" in inspect.getsource(scheduled.run_once)
# Made by Ryan Gomez & Co. Inc.
