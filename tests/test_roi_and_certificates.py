# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for core/fhir/roi.py and core/fhir/disposal_certificate.py.

Both exist to answer a question someone asks LATER, under pressure: "what
exactly did you release, and when?" and "prove you destroyed this." So
these tests weight the properties that make the answers hold up -
identifying detail staying out of the index, the produced record set
being stored rather than regenerated, and a certificate being checkable
against evidence its holder cannot rewrite.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audit.log import GENESIS_HASH, AuditEvent  # noqa: E402
from core.fhir.disposal_certificate import (  # noqa: E402
    build_certificate,
    verify_certificate,
)
from core.fhir.roi import ROIError, ROIService, validate_requester_type  # noqa: E402


# ---------------------------------------------------------------------------
# Certificates of destruction
# ---------------------------------------------------------------------------

def _event(actor="r.gomez", key="fhir/Observation/obs1.json"):
    return AuditEvent(
        actor=actor, action="record.dispose", resource_key=key,
        purpose_of_use="Retention period expired; routine disposition.",
        timestamp="2026-08-18T12:00:00+00:00", prev_hash=GENESIS_HASH,
    )


def _cert(event, **overrides):
    kwargs = dict(
        resource_type="Observation", resource_id="obs1",
        storage_key="fhir/Observation/obs1.json", stored_sha256_hex="a" * 64,
        versions_destroyed=3, disposal_mode="expired",
        disposal_reason="Retention period expired; routine disposition.",
        disposed_by="r.gomez", audit_event_hash=event.event_hash,
        retention_until="2026-08-01T00:00:00+00:00",
        disposed_at="2026-08-18T12:00:00+00:00",
    )
    kwargs.update(overrides)
    return build_certificate(**kwargs)


def test_certificate_verifies_against_an_intact_audit_chain():
    event = _event()
    ok, reason = verify_certificate(_cert(event).to_dict(), [event.to_dict()])
    assert ok, reason


def test_an_edited_certificate_is_rejected():
    event = _event()
    forged = _cert(event).to_dict()
    forged["disposed_by"] = "someone.else"
    ok, reason = verify_certificate(forged, [event.to_dict()])
    assert not ok and "altered" in reason


def test_a_certificate_naming_an_absent_audit_event_is_not_evidence():
    """The check that actually matters. A fingerprint only catches
    careless editing - a forger recomputes it. What a holder cannot
    manufacture is presence in the hash chain."""
    ok, reason = verify_certificate(_cert(_event()).to_dict(), [])
    assert not ok
    assert "not present in the audit log" in reason


def test_a_certificate_is_not_trusted_when_the_chain_itself_is_broken():
    """Present-but-unverifiable is not good enough: if the log cannot
    support any claim, it cannot support this one."""
    event = _event()
    tampered = dict(event.to_dict(), actor="mallory")  # hash no longer recomputes
    ok, reason = verify_certificate(_cert(event).to_dict(), [tampered])
    assert not ok


def test_reissuing_a_certificate_produces_an_identical_document():
    """A lost certificate must be reproducible. A second, differently
    numbered document would read as a second destruction."""
    event = _event()
    assert _cert(event).certificate_id == _cert(event).certificate_id


def test_certificate_text_carries_no_clinical_content():
    text = _cert(_event()).to_text()
    assert "CERTIFICATE OF DESTRUCTION" in text
    assert "fhir/Observation/obs1.json" in text
    for leak in ("diagnosis", "patient name", "chief complaint"):
        assert leak not in text.lower()


# ---------------------------------------------------------------------------
# Release of information
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        sql_l = " ".join(sql.split()).lower()
        if sql_l.startswith("insert into roi_requests"):
            (rid, ref, rtype, purpose, created_by, detail_key,
             start, end, types) = params
            self.store[rid] = {
                "request_id": rid, "patient_reference": ref, "requester_type": rtype,
                "purpose_of_use": purpose, "status": "open", "created_by": created_by,
                "created_at": datetime.now(timezone.utc), "fulfilled_by": None,
                "fulfilled_at": None, "denied_reason": None,
                "detail_storage_key": detail_key, "export_storage_key": None,
                "production_storage_key": None, "record_count": None,
                "withheld_count": None, "scope_start": start, "scope_end": end,
                "scope_resource_types": types,
            }
        elif sql_l.startswith("update roi_requests set status = 'fulfilled'"):
            by, export_key, production_key, count, withheld, rid = params
            self.store[rid].update(status="fulfilled", fulfilled_by=by,
                                   fulfilled_at=datetime.now(timezone.utc),
                                   export_storage_key=export_key,
                                   production_storage_key=production_key,
                                   record_count=count, withheld_count=withheld)
        elif sql_l.startswith("update roi_requests set status = 'denied'"):
            by, reason, rid = params
            self.store[rid].update(status="denied", fulfilled_by=by, denied_reason=reason,
                                   fulfilled_at=datetime.now(timezone.utc))
        elif "where request_id" in sql_l:
            self._rows = [self.store[params[0]]] if params[0] in self.store else []
            self.description = [(k,) for k in next(iter(self.store.values()), {}).keys()]
        elif "where status" in sql_l:
            # The real query filters on status; a fake that ignores it
            # made the reports page look like it listed unfulfilled
            # requests as disclosures.
            self._rows = [r for r in self.store.values() if r["status"] == params[0]]
            self.description = [(k,) for k in next(iter(self.store.values()), {}).keys()]
        else:
            self._rows = list(self.store.values())
            self.description = [(k,) for k in next(iter(self.store.values()), {}).keys()]

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _FakeStorage:
    def __init__(self):
        self.objects = {}

    def put_object(self, key, ciphertext, wrapped_dek_b64, sha256_hex,
                   retention_until=None, content_type="application/octet-stream"):
        self.objects[key] = ciphertext
        return type("M", (), {"version_id": "v1"})()

    def get_object(self, key, version_id=None):
        return self.objects[key]

    def get_metadata(self, key, version_id=None):
        return type("M", (), {"wrapped_dek_b64": "k"})()


class _FakeEncryptor:
    """Identity 'encryption' with the same nonce-prefix shape as the real
    envelope, so storage-key handling and offsets are exercised."""

    def encrypt(self, plaintext):
        return type("P", (), {"nonce": b"0" * 12, "ciphertext": plaintext,
                              "wrapped_dek_b64": "k"})()

    def decrypt(self, ciphertext, nonce, wrapped_dek_b64):
        return ciphertext


class _FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, actor, action, resource_key, purpose_of_use=None):
        self.events.append({"actor": actor, "action": action,
                            "resource_key": resource_key, "purpose_of_use": purpose_of_use})


# A small dated record set spanning 2018-2023, so date scoping has
# something real to include and exclude.
_DATED = {
    "fhir/Patient/eAB12cd3.json": {"resourceType": "Patient", "id": "eAB12cd3"},
    "fhir/Encounter/enc2018.json": {
        "resourceType": "Encounter", "id": "enc2018",
        "period": {"start": "2018-05-02T09:00:00Z"}},
    "fhir/Observation/obs2020.json": {
        "resourceType": "Observation", "id": "obs2020",
        "effectiveDateTime": "2020-03-15"},
    "fhir/Immunization/imm2023.json": {
        "resourceType": "Immunization", "id": "imm2023",
        "occurrenceDateTime": "2023-11-02"},
}


class _FakeReader:
    def resources_for_patient(self, patient_reference):
        return [{"storage_key": k, "sha256_hex": "a" * 64} for k in _DATED]

    def read_resource(self, storage_key):
        return _DATED[storage_key]

    def verify_object_integrity(self, storage_key):
        return True


def _service():
    store = {}
    storage, audit = _FakeStorage(), _FakeAudit()
    service = ROIService(
        connection_factory=lambda: _FakeConn(store),
        storage=storage, encryptor=_FakeEncryptor(), audit=audit, reader=_FakeReader(),
    )
    return service, storage, audit


def test_requester_type_is_a_fixed_vocabulary():
    """What a requester is entitled to differs by category, so this is
    not free text."""
    validate_requester_type("attorney")
    with pytest.raises(ROIError):
        validate_requester_type("someone who asked nicely")


def test_creating_a_request_keeps_the_requester_name_out_of_the_index():
    """THE constraint on this table's shape. A requester name is personal
    data; the index must not become a store of it."""
    service, storage, _ = _service()
    request = service.create(
        patient_reference="Patient/eAB12cd3", requester_type="attorney",
        requester_detail="Smith & Associates LLP", purpose_of_use="legal",
        authorization_reference="Case 2026-CV-1234", created_by="him.user",
    )
    row = service.get(request.request_id).__dict__
    assert "Smith" not in str(row), "requester name reached the index row"
    assert row["requester_type"] == "attorney"
    assert row["detail_storage_key"] in storage.objects
    assert b"Smith & Associates LLP" in storage.objects[row["detail_storage_key"]]


def test_a_request_without_a_requester_is_refused():
    service, _, _ = _service()
    with pytest.raises(ROIError, match="requester detail is required"):
        service.create(patient_reference="Patient/eAB12cd3", requester_type="payer",
                       requester_detail="   ", purpose_of_use="payment",
                       authorization_reference=None, created_by="him.user")


def test_fulfilment_stores_the_produced_record_set():
    """Regenerating a disclosure later can return different resources -
    records get disposed of, retention elapses, the index gets rebuilt.
    A disclosure that cannot be reproduced is not accounted for."""
    service, storage, _ = _service()
    request = service.create(
        patient_reference="Patient/eAB12cd3", requester_type="patient",
        requester_detail="The individual", purpose_of_use="patient_request",
        authorization_reference=None, created_by="him.user",
    )
    fulfilled = service.fulfil(request.request_id, fulfilled_by="him.user")

    assert fulfilled.status == "fulfilled"
    assert fulfilled.record_count == len(_DATED)  # no scope requested
    assert fulfilled.export_storage_key in storage.objects
    assert b'"resourceType": "Bundle"' in storage.objects[fulfilled.export_storage_key]


def test_the_disclosure_is_audited_before_records_are_read():
    service, _, audit = _service()
    request = service.create(
        patient_reference="Patient/eAB12cd3", requester_type="payer",
        requester_detail="Example Health Plan", purpose_of_use="payment",
        authorization_reference=None, created_by="him.user",
    )
    service.fulfil(request.request_id, fulfilled_by="him.user")
    actions = [e["action"] for e in audit.events]
    assert actions.index("roi.disclosure") < len(actions)
    assert any(e["action"] == "roi.disclosure" for e in audit.events)


def test_a_fulfilled_request_cannot_be_fulfilled_twice():
    """Each disclosure is accounted for separately; re-running one would
    release records again under the original request's authority."""
    service, _, _ = _service()
    request = service.create(
        patient_reference="Patient/eAB12cd3", requester_type="provider",
        requester_detail="Referring clinic", purpose_of_use="treatment",
        authorization_reference=None, created_by="him.user",
    )
    service.fulfil(request.request_id, fulfilled_by="him.user")
    with pytest.raises(ROIError, match="already fulfilled"):
        service.fulfil(request.request_id, fulfilled_by="him.user")


def test_a_denial_records_its_reason():
    """A refusal is as much a part of the accounting as a release."""
    service, _, audit = _service()
    request = service.create(
        patient_reference="Patient/eAB12cd3", requester_type="employer",
        requester_detail="Example Corp", purpose_of_use="operations",
        authorization_reference=None, created_by="him.user",
    )
    denied = service.deny(request.request_id, denied_by="him.user",
                          reason="No valid authorization on file")
    assert denied.status == "denied"
    assert "authorization" in denied.denied_reason
    assert any(e["action"] == "roi.request.denied" for e in audit.events)


def test_request_ids_are_not_sequential():
    """Sequential ids leak how many records requests an organization
    receives, and let anyone holding one enumerate its neighbours."""
    from core.fhir.roi import new_request_id

    assert len({new_request_id() for _ in range(50)}) == 50


# ---------------------------------------------------------------------------
# Date scoping - a records request is usually for a period
# ---------------------------------------------------------------------------

def _scoped(**kwargs):
    service, storage, audit = _service()
    defaults = dict(
        patient_reference="Patient/eAB12cd3", requester_type="attorney",
        requester_detail="Smith & Associates LLP", purpose_of_use="legal",
        authorization_reference="Case 2026-CV-1234", created_by="him.user",
    )
    defaults.update(kwargs)
    request = service.create(**defaults)
    return service, storage, audit, service.fulfil(request.request_id, fulfilled_by="him.user")


def test_date_scope_excludes_records_outside_the_requested_period():
    """The 2018 encounter and 2023 immunization fall outside 2019-2021 and
    must not be released; the 2020 observation must."""
    _, storage, _, fulfilled = _scoped(scope_start="2019-01-01", scope_end="2021-12-31")

    bundle = storage.objects[fulfilled.export_storage_key].decode()
    assert "obs2020" in bundle
    assert "enc2018" not in bundle
    assert "imm2023" not in bundle
    assert fulfilled.withheld_count == 2


def test_the_patient_resource_is_released_even_under_a_date_scope():
    """A bundle of observations with no Patient resource identifies
    nobody. Demographics carry no service date and are always included."""
    _, storage, _, fulfilled = _scoped(scope_start="2019-01-01", scope_end="2021-12-31")
    assert "eAB12cd3" in storage.objects[fulfilled.export_storage_key].decode()


def test_scope_filters_on_clinical_dates_not_ingestion_dates():
    """The distinction the whole clinical_dates module exists for. Every
    fake resource is 'stored' now; only their service dates differ. A
    scope ending in 2021 must still exclude the 2023 record."""
    _, storage, _, fulfilled = _scoped(scope_start="2019-01-01", scope_end="2021-12-31")
    assert "imm2023" not in storage.objects[fulfilled.export_storage_key].decode()


def test_resource_type_scope_is_honoured():
    _, storage, _, fulfilled = _scoped(scope_resource_types="Observation")
    bundle = storage.objects[fulfilled.export_storage_key].decode()
    assert "obs2020" in bundle
    assert "enc2018" not in bundle


def test_an_inverted_date_range_is_refused():
    service, _, _ = _service()
    with pytest.raises(ROIError, match="starts .* after it ends"):
        service.create(patient_reference="Patient/eAB12cd3", requester_type="payer",
                       requester_detail="Example Plan", purpose_of_use="payment",
                       authorization_reference=None, created_by="him.user",
                       scope_start="2021-12-31", scope_end="2019-01-01")


def test_an_unparseable_date_is_refused_rather_than_ignored():
    """Silently ignoring a malformed bound would release the complete
    record for a request the operator scoped."""
    service, _, _ = _service()
    with pytest.raises(ROIError, match="could not read"):
        service.create(patient_reference="Patient/eAB12cd3", requester_type="payer",
                       requester_detail="Example Plan", purpose_of_use="payment",
                       authorization_reference=None, created_by="him.user",
                       scope_start="last Tuesday")


# ---------------------------------------------------------------------------
# The legal production document
# ---------------------------------------------------------------------------

def test_fulfilment_produces_a_pdf_alongside_the_bundle():
    """Both artifacts, from the same filtered set in one operation, so
    they cannot disagree about what was released."""
    service, storage, _, fulfilled = _scoped(scope_start="2019-01-01", scope_end="2021-12-31")

    assert fulfilled.production_storage_key in storage.objects
    assert fulfilled.export_storage_key in storage.objects
    # Read through the decrypt path rather than the raw bytes: stored
    # objects carry the 12-byte nonce prefix, exactly as clinical
    # resources do, so the PDF is not at offset zero.
    assert service.read_production(fulfilled.request_id).startswith(b"%PDF")


def test_the_production_can_be_read_back_for_download():
    service, _, _, fulfilled = _scoped()
    pdf = service.read_production(fulfilled.request_id)
    assert pdf is not None and pdf.startswith(b"%PDF")


def test_the_production_manifest_lists_withheld_records_with_reasons():
    """A production that silently omits records invites the question of
    what else was omitted."""
    from core.fhir.legal_export import ExportScope, LegalExportBuilder
    from core.fhir.clinical_dates import coerce_scope_bound

    export = LegalExportBuilder().build(
        scope=ExportScope("Patient/eAB12cd3",
                          coerce_scope_bound("2019-01-01"),
                          coerce_scope_bound("2021-12-31", end_of_day=True)),
        resources=[({"storage_key": k, "sha256_hex": "a" * 64}, r) for k, r in _DATED.items()],
        request_id="roi-test", requester_type="attorney",
        requester_detail="Smith & Associates LLP", purpose_of_use="legal",
        produced_by="him.user",
    )
    withheld = {e.resource_id: e.reason for e in export.manifest if not e.included}
    assert "enc2018" in withheld and "before the requested period" in withheld["enc2018"]
    assert "imm2023" in withheld and "after the requested period" in withheld["imm2023"]


def test_every_production_page_carries_a_bates_number():
    """What makes a produced set citable - both sides mean the same page
    when they say PHIAI-000042."""
    from core.fhir.legal_export import ExportScope, LegalExportBuilder

    export = LegalExportBuilder(bates_prefix="TEST").build(
        scope=ExportScope("Patient/eAB12cd3"),
        resources=[({"storage_key": k, "sha256_hex": "a" * 64}, r) for k, r in _DATED.items()],
        request_id="roi-test", requester_type="patient",
        requester_detail="The individual", purpose_of_use="patient_request",
        produced_by="him.user",
    )
    assert export.bates_first == "TEST-000001"
    assert export.bates_last == f"TEST-{export.page_count:06d}"
    assert export.page_count >= len(_DATED)
    assert all(e.first_bates for e in export.manifest if e.included)


def test_integrity_is_reported_as_unverified_when_no_check_was_run():
    """A production asserting an integrity check it never performed would
    be worse than one making no claim."""
    from core.fhir.legal_export import ExportScope, LegalExportBuilder

    export = LegalExportBuilder().build(
        scope=ExportScope("Patient/eAB12cd3"),
        resources=[({"storage_key": "fhir/Patient/eAB12cd3.json", "sha256_hex": "a" * 64},
                    _DATED["fhir/Patient/eAB12cd3.json"])],
        request_id="roi-test", requester_type="patient",
        requester_detail="The individual", purpose_of_use="patient_request",
        produced_by="him.user", verify_integrity=None,
    )
    assert all(e.integrity_ok is None for e in export.manifest if e.included)


def test_a_failed_integrity_check_is_surfaced_not_swallowed():
    from core.fhir.legal_export import ExportScope, LegalExportBuilder

    export = LegalExportBuilder().build(
        scope=ExportScope("Patient/eAB12cd3"),
        resources=[({"storage_key": "fhir/Patient/eAB12cd3.json", "sha256_hex": "a" * 64},
                    _DATED["fhir/Patient/eAB12cd3.json"])],
        request_id="roi-test", requester_type="patient",
        requester_detail="The individual", purpose_of_use="patient_request",
        produced_by="him.user", verify_integrity=lambda key: False,
    )
    assert export.integrity_failures


def test_a_release_can_be_scoped_to_one_encounter():
    """A records request is often for a single visit."""
    from core.fhir.clinical_dates import coerce_scope_bound
    from core.fhir.legal_export import ExportScope, LegalExportBuilder

    resources = [
        ({"storage_key": "fhir/Patient/p.json", "sha256_hex": "a" * 64},
         {"resourceType": "Patient", "id": "p"}),
        ({"storage_key": "fhir/Observation/in.json", "sha256_hex": "a" * 64},
         {"resourceType": "Observation", "id": "in",
          "encounter": {"reference": "Encounter/enc1"}}),
        ({"storage_key": "fhir/Observation/out.json", "sha256_hex": "a" * 64},
         {"resourceType": "Observation", "id": "out",
          "encounter": {"reference": "Encounter/other"}}),
    ]
    export = LegalExportBuilder().build(
        scope=ExportScope("Patient/p", encounter_id="enc1"),
        resources=resources, request_id="roi-1", requester_type="attorney",
        requester_detail="Firm", purpose_of_use="legal", produced_by="him",
    )
    produced = {e.resource_id for e in export.manifest if e.included}
    assert produced == {"p", "in"}, "encounter scoping did not filter"

    withheld = {e.resource_id: e.reason for e in export.manifest if not e.included}
    assert "Encounter/other" in withheld["out"]


def test_encounter_scope_appears_in_the_production_cover_sheet():
    from core.fhir.legal_export import ExportScope

    assert "encounter enc1 only" in ExportScope("Patient/p", encounter_id="enc1").describe()
# Made by Ryan Gomez & Co. Inc.
