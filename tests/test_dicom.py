# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Tests for DICOM ingestion and the DICOMweb API the viewer talks to.

Weighted toward the boundary rather than the plumbing, matching
tests/test_web.py: that a DICOM file survives storage byte-for-byte,
that imaging cannot be read without a permission AND a stated purpose,
that the auditor role still cannot see clinical content, and that a
disclosure is recorded before anything is decrypted.

Real DICOM is synthesised with pydicom rather than committed as a
fixture: a checked-in .dcm from anywhere real is PHI, and one built here
is inspectable, deterministic, and cannot quietly be a patient's scan.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.dicom import dicomweb, index as imaging_index, model  # noqa: E402
from core.dicom.ingest import DICOMIngestor, iter_dicom_files  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_dicom(
    patient_id="eAB12cd3",
    patient_name="DOE^JANE",
    study_uid="1.2.840.113619.2.1",
    series_uid="1.2.840.113619.2.1.1",
    sop_uid="1.2.840.113619.2.1.1.1",
    modality="CT",
    rows=4,
    columns=4,
    frames=1,
) -> bytes:
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

    ds = Dataset()
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "F"
    ds.AccessionNumber = "ACC123"
    ds.StudyDate = "20240315"
    ds.StudyTime = "101500"
    ds.StudyDescription = "CT CHEST W CONTRAST"
    ds.ReferringPhysicianName = "SMITH^JOHN"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CTImageStorage
    ds.Modality = modality
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.SeriesDescription = "AXIAL"
    ds.BodyPartExamined = "CHEST"
    ds.Rows, ds.Columns = rows, columns
    ds.BitsAllocated = 8
    ds.SamplesPerPixel = 1
    if frames > 1:
        ds.NumberOfFrames = frames
    # Distinct bytes per frame so frame extraction can be checked.
    ds.PixelData = bytes(
        bytearray(
            (frame + 1) for frame in range(frames) for _ in range(rows * columns)
        )
    )

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm

    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


class _FakeKMS:
    def generate_data_key(self):
        return b"\x01" * 32, "d3JhcHBlZA=="

    def unwrap_data_key(self, wrapped_dek_b64):
        return b"\x01" * 32


class _Stored:
    def __init__(self, version_id="v1"):
        self.version_id = version_id
        self.wrapped_dek_b64 = "d3JhcHBlZA=="


class _FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.meta: dict[str, str] = {}

    def put_object(self, key, ciphertext, wrapped_dek_b64, sha256_hex,
                   retention_until=None, content_type=""):
        self.objects[key] = ciphertext
        self.meta[key] = wrapped_dek_b64
        return _Stored()

    def get_object(self, key, version_id=None):
        return self.objects[key]

    def get_metadata(self, key, version_id=None):
        return _Stored()

    def object_exists(self, key):
        return key in self.objects


class _RecordingAudit:
    def __init__(self):
        self.events = []

    def record(self, actor, action, resource_key, purpose_of_use=None):
        self.events.append(
            {"actor": actor, "action": action, "resource_key": resource_key,
             "purpose_of_use": purpose_of_use}
        )


@pytest.fixture
def encryptor():
    from core.crypto.envelope import EnvelopeEncryptor

    return EnvelopeEncryptor(kms=_FakeKMS())


@pytest.fixture
def export(tmp_path):
    """A small PACS-export-shaped directory."""
    root = tmp_path / "export"
    (root / "STUDY1" / "SER1").mkdir(parents=True)
    (root / "STUDY1" / "SER1" / "IM000001").write_bytes(
        make_dicom(sop_uid="1.2.840.113619.2.1.1.1")
    )
    (root / "STUDY1" / "SER1" / "IM000002.dcm").write_bytes(
        make_dicom(sop_uid="1.2.840.113619.2.1.1.2")
    )
    (root / "STUDY1" / "notes.txt").write_text("not dicom")
    return root


# ---------------------------------------------------------------------------
# Discovery and parsing
# ---------------------------------------------------------------------------


def test_dicom_files_are_found_by_magic_not_extension(export):
    found = sorted(p.name for p in iter_dicom_files(export))
    # IM000001 has no extension at all - a PACS export convention - and
    # notes.txt is not DICOM whatever it is called.
    assert found == ["IM000001", "IM000002.dcm"]


def test_a_dicomdir_index_is_not_ingested_as_an_image(export):
    (export / "DICOMDIR").write_bytes(make_dicom(sop_uid="1.2.3.9"))
    assert "DICOMDIR" not in [p.name for p in iter_dicom_files(export)]


def test_records_carry_the_identifying_metadata_a_worklist_needs():
    import pydicom

    ds = pydicom.dcmread(io.BytesIO(make_dicom()), stop_before_pixels=True)
    study, series, instance = model.read_records(ds, size_bytes=100)

    assert study.patient_name == "DOE^JANE"
    assert study.patient_id == "eAB12cd3"
    # This is what joins imaging to the FHIR side of the platform.
    assert study.patient_reference == "Patient/eAB12cd3"
    assert study.accession_number == "ACC123"
    assert series.modality == "CT"
    assert instance.number_of_frames == 1


def test_a_file_missing_a_uid_is_rejected_rather_than_given_one():
    import pydicom

    ds = pydicom.dcmread(io.BytesIO(make_dicom()), stop_before_pixels=True)
    del ds.SeriesInstanceUID

    with pytest.raises(ValueError, match="SeriesInstanceUID"):
        model.read_records(ds, size_bytes=100)


def test_the_object_key_mirrors_the_dicom_hierarchy():
    assert model.instance_key("1.2", "1.2.3", "1.2.3.4") == "dicom/1.2/1.2.3/1.2.3.4.dcm"
    assert model.study_prefix("1.2") == "dicom/1.2/"


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_a_study_is_stored_encrypted_and_byte_for_byte(export, encryptor):
    storage, audit = _FakeStorage(), _RecordingAudit()
    result = DICOMIngestor(storage, encryptor, audit).ingest_directory(export)

    assert result.instance_count == 2
    assert len(result.studies) == 1

    key = model.instance_key(
        "1.2.840.113619.2.1", "1.2.840.113619.2.1.1", "1.2.840.113619.2.1.1.1"
    )
    assert key in storage.objects

    # Stored ciphertext is not the plaintext.
    original = (export / "STUDY1" / "SER1" / "IM000001").read_bytes()
    assert storage.objects[key] != original

    # And it round-trips EXACTLY - a system that normalises a DICOM file
    # on the way in is a system that has altered the record.
    stored = storage.objects[key]
    recovered = encryptor.decrypt(stored[12:], stored[:12], "d3JhcHBlZA==")
    assert recovered == original


def test_imaging_is_audited_per_study_not_per_instance(export, encryptor):
    audit = _RecordingAudit()
    DICOMIngestor(_FakeStorage(), encryptor, audit).ingest_directory(export)

    # Two instances, one study, one audit entry - see the module docstring
    # for why per-instance auditing is the wrong granularity here.
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event["action"] == "record.write.imaging.study"
    assert "2 instances" in event["resource_key"]


def test_reimporting_skips_what_is_already_stored(export, encryptor):
    storage, audit = _FakeStorage(), _RecordingAudit()
    ingestor = DICOMIngestor(storage, encryptor, audit)

    ingestor.ingest_directory(export)
    again = ingestor.ingest_directory(export)

    assert again.instance_count == 0
    # core/dicom/ingest.py's skip reason, pinned verbatim: that module
    # appends the literal "already stored" (ingest.py's _store_instance),
    # and core/dicom/__main__.py's _ALREADY_STORED counts on the same
    # value to tell a resumed-import skip apart from an unreadable file.
    # All three move together or not at all.
    assert all(reason == "already stored" for _, reason in again.skipped)


def test_an_unreadable_file_does_not_stop_the_import(export, encryptor):
    # A file with the DICM magic but a corrupt body - a real occurrence in
    # an old export, and one bad slice must not abandon the study.
    (export / "STUDY1" / "SER1" / "CORRUPT").write_bytes(b"\x00" * 128 + b"DICM" + b"\xff" * 32)

    result = DICOMIngestor(_FakeStorage(), encryptor, _RecordingAudit()).ingest_directory(export)

    assert result.instance_count == 2
    assert any("CORRUPT" in path for path, _ in result.skipped)


# ---------------------------------------------------------------------------
# DICOMweb representation
# ---------------------------------------------------------------------------


def test_study_json_is_dicom_json_with_the_right_tags():
    row = {
        "study_instance_uid": "1.2.3", "study_date": "20240315",
        "patient_name": "DOE^JANE", "patient_id": "eAB12cd3",
        "modalities": "CT", "series_count": 2, "instance_count": 40,
        "accession_number": None,
    }
    out = dicomweb.study_json(row)

    assert out["0020000D"] == {"vr": "UI", "Value": ["1.2.3"]}
    # Person Name is the one VR whose JSON value is a component object.
    assert out["00100010"] == {"vr": "PN", "Value": [{"Alphabetic": "DOE^JANE"}]}
    assert out["00201208"] == {"vr": "IS", "Value": [40]}
    # An absent attribute is omitted, never sent as null or an empty array
    # - a viewer reading Value[0] on it would crash on older studies.
    assert "00080050" not in out


def test_instance_metadata_never_carries_pixel_data():
    metadata = dicomweb.instance_metadata(make_dicom())

    assert "7FE00010" not in metadata, "pixel data must not be inlined in metadata"
    assert metadata["00100020"]["Value"] == ["eAB12cd3"]


def test_frames_are_extracted_by_number():
    raw = make_dicom(rows=2, columns=2, frames=3)

    frames = list(dicomweb.frame_bytes(raw, [1, 3]))

    assert len(frames) == 2
    assert frames[0] == bytes([1, 1, 1, 1])
    assert frames[1] == bytes([3, 3, 3, 3])


def test_a_frame_out_of_range_is_refused():
    with pytest.raises(ValueError, match="out of range"):
        list(dicomweb.frame_bytes(make_dicom(frames=2), [5]))


# ---------------------------------------------------------------------------
# QIDO matching semantics
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [("study_instance_uid",)]

    def execute(self, sql, params=()):
        self.conn.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass


def test_dicom_wildcards_become_sql_wildcards():
    conn = _FakeConn()
    imaging_index.search_studies(conn, {"PatientName": "DOE*"})

    sql, params = conn.executed[0]
    assert "patient_name ILIKE" in sql
    assert "DOE%" in params


def test_a_literal_percent_in_a_name_is_not_a_wildcard():
    conn = _FakeConn()
    imaging_index.search_studies(conn, {"PatientName": "50%*"})

    _, params = conn.executed[0]
    # The literal % is escaped; only the DICOM * became a SQL wildcard.
    assert r"50\%%" in params


def test_a_date_range_becomes_two_bounds():
    conn = _FakeConn()
    imaging_index.search_studies(conn, {"StudyDate": "20240101-20241231"})

    sql, params = conn.executed[0]
    assert "study_date >= %s" in sql and "study_date <= %s" in sql
    assert "20240101" in params and "20241231" in params


def test_an_open_ended_date_range_uses_one_bound():
    conn = _FakeConn()
    imaging_index.search_studies(conn, {"StudyDate": "20240101-"})

    sql, _ = conn.executed[0]
    assert "study_date >= %s" in sql
    assert "study_date <= %s" not in sql


def test_a_server_side_patient_restriction_cannot_be_widened_by_the_viewer():
    conn = _FakeConn()
    imaging_index.search_studies(
        conn, {"PatientID": "someone-else"}, patient_reference="Patient/eAB12cd3"
    )

    sql, params = conn.executed[0]
    # Both are applied, ANDed - the viewer's filter narrows, never widens.
    assert "patient_id =" in sql and "patient_reference =" in sql
    assert "Patient/eAB12cd3" in params


def test_an_unsupported_search_key_is_ignored_not_rejected():
    """PS3.18 permits supporting a subset; failing the whole query because
    of one optional key would make the viewer look broken."""
    conn = _FakeConn()
    imaging_index.search_studies(conn, {"InstitutionName": "SOMEWHERE"})

    sql, _ = conn.executed[0]
    assert "WHERE" not in sql


def test_the_result_limit_is_capped():
    conn = _FakeConn()
    imaging_index.search_studies(conn, {}, limit=10**6)

    _, params = conn.executed[0]
    assert imaging_index.MAX_LIMIT in params


# ---------------------------------------------------------------------------
# The DICOMweb API: permission, purpose, audit
# ---------------------------------------------------------------------------


class _IndexConn:
    """A connection returning canned imaging index rows."""

    ROWS = {
        "studies": [{"study_instance_uid": "1.2.3", "patient_name": "DOE^JANE",
                     "patient_id": "eAB12cd3", "study_date": "20240315",
                     "modalities": "CT", "series_count": 1, "instance_count": 1}],
        "instances": [{"sop_instance_uid": "1.2.3.4.5", "series_instance_uid": "1.2.3.4",
                       "study_instance_uid": "1.2.3", "sop_class_uid": "1.2.840.10008.5.1.4.1.1.2",
                       "instance_number": 1, "number_of_frames": 1, "rows": 4, "columns": 4,
                       "bits_allocated": 8, "storage_key": "dicom/1.2.3/1.2.3.4/1.2.3.4.5.dcm",
                       "transfer_syntax_uid": "1.2.840.10008.1.2.1"}],
    }

    def __init__(self):
        self.closed = False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        flat = " ".join(sql.split()).lower()
        self._rows = self.ROWS["studies"] if "from dicom_studies" in flat else (
            self.ROWS["instances"] if "from dicom_instances" in flat else []
        )
        self.description = [(k,) for k in (self._rows[0] if self._rows else {"x": 1})]

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]

    def close(self):
        self.closed = True


class _ImagingReader:
    def __init__(self):
        self.reads = []

    def read_object_bytes(self, storage_key):
        self.reads.append(storage_key)
        return make_dicom(sop_uid="1.2.3.4.5", study_uid="1.2.3", series_uid="1.2.3.4")

    # Unused by imaging, present so create_app is satisfied.
    def stats(self):
        from core.web.data import PlatformStats

        return PlatformStats(0, {}, 0, None, None)

    def verify_audit_chain(self):
        return (True, 0, None)

    def read_audit_events(self, limit=200, actor=None):
        return []

    def expiring_resources(self, within_days=90):
        return []

    def search_patients(self, term, limit=50):
        return []

    def resources_for_patient(self, patient_reference):
        return []


def _imaging_app(roles="viewer", viewer_origin=None, monkeypatch=None):
    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings

    if viewer_origin is not None:
        import os

        os.environ["PHI_AI_IMAGING_VIEWER_ORIGIN"] = viewer_origin
    else:
        import os

        os.environ.pop("PHI_AI_IMAGING_VIEWER_ORIGIN", None)

    reader, audit = _ImagingReader(), _RecordingAudit()
    app = create_app(
        reader=reader,
        auth_settings=AuthSettings(trust_proxy_headers=False, dev_identity=f"tester:{roles}"),
        audit=audit,
        session_secret_key="test-secret",
        secure_cookies=False,
        imaging_connection_factory=lambda: _IndexConn(),
    )
    return TestClient(app), reader, audit


def _csrf(http) -> str:
    """The session's CSRF token, from the meta tag base.html always emits."""
    import re

    match = re.search(r'name="csrf-token" content="([^"]+)"', http.get("/").text)
    assert match, "no CSRF token on the page"
    return match.group(1)


def _with_purpose(http, purpose="treatment"):
    """Do what the platform's own 'open in viewer' action does."""
    import os

    os.environ.setdefault("PHI_AI_IMAGING_VIEWER_URL", "https://viewer.example.org")
    response = http.post(
        "/imaging/open",
        data={"study_instance_uid": "1.2.3", "purpose_of_use": purpose,
              "csrf_token": _csrf(http)},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    return response


def test_dicomweb_is_not_mounted_when_imaging_is_unconfigured():
    from fastapi.testclient import TestClient

    from core.web.app import create_app
    from core.web.auth import AuthSettings

    app = create_app(
        reader=_ImagingReader(),
        auth_settings=AuthSettings(trust_proxy_headers=False, dev_identity="tester:viewer"),
        audit=_RecordingAudit(),
        session_secret_key="s",
        secure_cookies=False,
    )
    assert TestClient(app).get("/dicomweb/studies").status_code == 404


def test_imaging_needs_a_purpose_of_use_recorded_first():
    http, reader, _ = _imaging_app()

    response = http.get("/dicomweb/studies/1.2.3/metadata")

    assert response.status_code == 403
    assert "purpose of use" in response.json()["detail"].lower()
    assert reader.reads == [], "nothing may be decrypted without a stated purpose"


def test_with_a_purpose_the_study_is_served_and_the_disclosure_recorded():
    http, reader, audit = _imaging_app()
    _with_purpose(http)

    response = http.get("/dicomweb/studies/1.2.3/metadata")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/dicom+json")
    assert reader.reads, "the instance should have been read"

    disclosure = [e for e in audit.events if e["action"] == "record.read.imaging.study"]
    assert len(disclosure) == 1
    assert disclosure[0]["purpose_of_use"] == "treatment"
    assert disclosure[0]["resource_key"] == "dicom/1.2.3/"


def test_the_disclosure_is_recorded_before_anything_is_decrypted():
    """Same ordering core/web/app.py uses: a failed audit must end the
    request having never decrypted PHI."""
    http, reader, audit = _imaging_app()
    _with_purpose(http)

    order = []

    original_record = audit.record
    original_read = reader.read_object_bytes

    def record(**kw):
        if kw.get("action") == "record.read.imaging.study":
            order.append("audit")
        return original_record(**kw)

    def read(key):
        order.append("decrypt")
        return original_read(key)

    audit.record, reader.read_object_bytes = record, read
    http.get("/dicomweb/studies/1.2.3/metadata")

    assert order[0] == "audit", f"decrypted before auditing: {order}"


def test_an_auditor_cannot_read_imaging():
    """The auditor-is-not-a-viewer rule holds for images too.

    Refused at BOTH gates, which is the point: the auditor cannot even
    state a purpose of use for imaging, so there is no sequence of
    requests that reaches a pixel.
    """
    http, reader, _ = _imaging_app(roles="auditor")

    opened = http.post(
        "/imaging/open",
        data={"study_instance_uid": "1.2.3", "purpose_of_use": "operations",
              "csrf_token": _csrf(http)},
    )
    assert opened.status_code == 403

    assert http.get("/dicomweb/studies/1.2.3/metadata").status_code == 403
    assert reader.reads == []


def test_a_malformed_uid_is_refused_before_any_lookup():
    http, reader, _ = _imaging_app()
    _with_purpose(http)

    for bad in ("../../etc/passwd", "1.2.3'; DROP TABLE dicom_studies--", "not-a-uid"):
        response = http.get(f"/dicomweb/studies/{bad}/metadata")
        assert response.status_code in (400, 404), bad
    assert reader.reads == []


def test_an_instance_is_served_as_application_dicom():
    http, _, _ = _imaging_app()
    _with_purpose(http)

    response = http.get("/dicomweb/studies/1.2.3/series/1.2.3.4/instances/1.2.3.4.5")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/dicom"
    # Byte-for-byte DICOM, decodable by the viewer.
    assert response.content[128:132] == b"DICM"
    assert response.headers["cache-control"] == "no-store"


def test_frames_come_back_as_multipart_related():
    http, _, _ = _imaging_app()
    _with_purpose(http)

    response = http.get(
        "/dicomweb/studies/1.2.3/series/1.2.3.4/instances/1.2.3.4.5/frames/1"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("multipart/related")
    assert b"transfer-syntax=1.2.840.10008.1.2.1" in response.content


def test_opening_a_study_without_the_permission_is_refused():
    http, _, _ = _imaging_app(roles="disposition")
    response = http.post(
        "/imaging/open",
        data={"study_instance_uid": "1.2.3", "purpose_of_use": "treatment",
              "csrf_token": _csrf(http)},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# CORS for the separate viewer origin
# ---------------------------------------------------------------------------


def test_cors_is_scoped_to_dicomweb_and_never_covers_the_record_pages():
    http, _, _ = _imaging_app(viewer_origin="https://viewer.example.org")
    origin = {"Origin": "https://viewer.example.org"}
    _with_purpose(http)

    imaging = http.get("/dicomweb/studies/1.2.3/metadata", headers=origin)
    assert imaging.headers.get("access-control-allow-origin") == "https://viewer.example.org"
    assert imaging.headers.get("access-control-allow-credentials") == "true"

    # The viewer origin gets NO cross-origin access to the platform itself.
    page = http.get("/patients", headers=origin)
    assert "access-control-allow-origin" not in page.headers


def test_an_unknown_origin_gets_no_cors_headers():
    http, _, _ = _imaging_app(viewer_origin="https://viewer.example.org")
    _with_purpose(http)

    response = http.get(
        "/dicomweb/studies/1.2.3/metadata", headers={"Origin": "https://evil.example"}
    )
    assert "access-control-allow-origin" not in response.headers


def test_preflight_is_answered_without_authentication():
    """A browser sends preflight with no credentials; requiring an
    identity would fail every cross-origin imaging request."""
    http, _, _ = _imaging_app(viewer_origin="https://viewer.example.org")

    response = http.request(
        "OPTIONS", "/dicomweb/studies", headers={"Origin": "https://viewer.example.org"}
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://viewer.example.org"


def test_the_record_page_offers_imaging_and_carries_the_purpose_forward():
    # viewer, not him: the role dictates the purposes it may assert, and
    # treatment belongs to treating roles.
    http, _, _ = _imaging_app(roles="viewer")

    body = http.post(
        "/patients/eAB12cd3/open",
        data={"purpose_of_use": "treatment", "csrf_token": _csrf(http)},
    ).text

    assert "Open in viewer" in body
    assert 'name="study_instance_uid" value="1.2.3"' in body
    # The purpose already stated to open the chart carries into imaging,
    # so both disclosures are audited under the same stated reason.
    assert 'name="purpose_of_use" value="treatment"' in body


def test_a_role_without_imaging_sees_no_studies():
    http, _, _ = _imaging_app(roles="disposition")
    body = http.get("/retention").text
    assert "Open in viewer" not in body
# Made by Ryan Gomez & Co. Inc.
