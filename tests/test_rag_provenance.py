# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The ingestion boundary (SPEC §5.1 d, h).

5.1(d) makes the caller's grant a predicate inside the scan and 5.1(h)
says purpose-of-use bounds retrieval scope. Both were threaded through
the pipeline and the audit, and neither reached GrantScope: the
boundary actually enforced was patient / encounter / resource type /
date. An answer could therefore be built from a record no one could
show was lawfully brought in, which is the claim the platform exists to
refute.

These tests were written against the same three questions that caught
the equivalent gap in the demo:

  1. does an unprovenanced chunk get in when the posture forbids it,
  2. does naming permitted sources actually EXCLUDE the others - the
     "is this predicate a no-op" question, which is the one a passing
     test suite is worst at answering, and
  3. does the default posture leave existing deployments alone.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.rag.retriever import GrantScope, retrieve  # noqa: E402
from core.rag.serialization import Chunk, Provenance  # noqa: E402

PATIENT = "Patient/alpha"


def _chunk(key: str, text: str, provenance: Provenance | None = None) -> Chunk:
    return Chunk(
        storage_key=key,
        resource_type="Condition",
        subject_reference=PATIENT,
        encounter_reference=None,
        effective="2026-01-01",
        clinical_status="active",
        verification_status="confirmed",
        codes=(("http://snomed.info/sct", "38341003", "Hypertension"),),
        template_version="2",
        text=text,
        provenance=provenance,
    )


EPIC = Provenance(source_system="epic", ingested_at="2026-09-02T10:00:00Z", run="run-7")
CERNER = Provenance(source_system="cerner", ingested_at="2026-09-02T10:00:00Z", run="run-7")


# --- 3. the default posture is unchanged ---------------------------------
def test_unprovenanced_chunk_is_admitted_by_default():
    """A deployment that has never recorded origin keeps working. The
    boundary is a posture the caller opts into, not a breaking upgrade."""
    scope = GrantScope(patient_reference=PATIENT)
    assert scope.admits(_chunk("k1", "hypertension", None))


# --- 1. absence of provenance is refusable -------------------------------
def test_require_provenance_rejects_a_chunk_with_no_origin():
    scope = GrantScope(patient_reference=PATIENT, require_provenance=True)
    assert not scope.admits(_chunk("k1", "hypertension", None))
    assert scope.admits(_chunk("k2", "hypertension", EPIC))


# --- 2. the predicate is not a no-op -------------------------------------
def test_permitted_sources_excludes_every_other_system():
    """The test that matters: a filter that admits everything passes any
    positive assertion you write about it. This asserts the negative."""
    scope = GrantScope(patient_reference=PATIENT, permitted_sources=frozenset({"epic"}))
    assert scope.admits(_chunk("k1", "hypertension", EPIC))
    assert not scope.admits(_chunk("k2", "hypertension", CERNER))


def test_permitted_sources_rejects_unprovenanced_chunks():
    """Naming permitted sources is itself a statement that origin must be
    known - an unprovenanced chunk cannot satisfy it by defaulting in."""
    scope = GrantScope(patient_reference=PATIENT, permitted_sources=frozenset({"epic"}))
    assert not scope.admits(_chunk("k1", "hypertension", None))


def test_retrieval_returns_only_permitted_sources():
    """End to end through retrieve(), because admits() passing in isolation
    does not prove the scan applies it."""
    chunks = [
        _chunk("epic-1", "patient has hypertension", EPIC),
        _chunk("cerner-1", "patient has hypertension", CERNER),
        _chunk("orphan-1", "patient has hypertension", None),
    ]
    unbounded = retrieve("hypertension", chunks, GrantScope(patient_reference=PATIENT))
    assert len(unbounded) == 3, "control: all three are otherwise retrievable"

    bounded = retrieve(
        "hypertension",
        chunks,
        GrantScope(patient_reference=PATIENT, permitted_sources=frozenset({"epic"})),
    )
    assert [c.storage_key for c, _ in bounded] == ["epic-1"]


def test_retrieval_with_required_provenance_drops_the_orphan():
    chunks = [
        _chunk("epic-1", "patient has hypertension", EPIC),
        _chunk("orphan-1", "patient has hypertension", None),
    ]
    got = retrieve(
        "hypertension",
        chunks,
        GrantScope(patient_reference=PATIENT, require_provenance=True),
    )
    assert [c.storage_key for c, _ in got] == ["epic-1"]


# --- provenance answers the disclosure question --------------------------
def test_provenance_names_system_run_and_time():
    """A storage key says where a fact is kept. Provenance says who handed
    it over, under which run, and when - the question asked when a
    disclosure is challenged."""
    chunk = _chunk("epic-1", "hypertension", EPIC)
    assert chunk.provenance.source_system == "epic"
    assert chunk.provenance.run == "run-7"
    assert chunk.provenance.ingested_at == "2026-09-02T10:00:00Z"


def test_serialize_resource_threads_provenance_onto_the_chunk():
    """The ETL is where origin is known - the resource itself never carries
    it. If the serializer drops it, every downstream guarantee here is
    enforcing a field that is always None."""
    from core.governance.segmentation import CategoryValueSets
    from core.rag.serialization import serialize_resource

    resource = {
        "resourceType": "Condition",
        "subject": {"reference": PATIENT},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
        "onsetDateTime": "2026-01-01",
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "38341003", "display": "Hypertension"}]},
    }
    result = serialize_resource(
        resource, "s3://bucket/cond-1", CategoryValueSets(), provenance=EPIC
    )
    assert result.chunk is not None, "control: this resource must serialize"
    assert result.chunk.provenance is EPIC
    assert result.chunk.provenance.source_system == "epic"


def test_serialize_resource_without_provenance_is_still_valid():
    from core.governance.segmentation import CategoryValueSets
    from core.rag.serialization import serialize_resource

    resource = {
        "resourceType": "Condition",
        "subject": {"reference": PATIENT},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "38341003", "display": "Hypertension"}]},
    }
    result = serialize_resource(resource, "s3://bucket/cond-2", CategoryValueSets())
    assert result.chunk is not None
    assert result.chunk.provenance is None


def test_provenance_is_frozen():
    with pytest.raises(Exception):
        EPIC.source_system = "cerner"  # type: ignore[misc]
# Made by Ryan Gomez & Co. Inc.
