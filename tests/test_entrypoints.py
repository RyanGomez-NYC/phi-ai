# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Every `settings.<attr>` an entry point reads must exist on Settings.

WHY THIS FILE EXISTS. `core/web/__main__.py`, `core/verify/__main__.py`
and `core/fhir/delivery/__main__.py` all connected to Postgres as
`settings.db_username` - a field that has never existed on the Settings
dataclass, which has `db_ingest_username` and `db_reader_username`
instead, deliberately separate because core/db/bootstrap_aws.sql grants
them different privileges. All three entry points therefore died with
AttributeError before doing any work: `python -m core.web` could not
serve a request, `python -m core.verify` could not verify, and
`python -m core.fhir.delivery` could not deliver.

The test suite did not catch it, and the reason is worth stating because
it generalises. Every web test injects a fake reader straight into
`create_app()`, precisely so the routes can be tested without Postgres,
S3 or a KMS. That dependency injection is good design and it is also why
`build()` - the function that wires the real thing together - had no
test executing it at all. The seam that makes the routes testable is
exactly the seam the wiring hides behind.

So this checks the wiring statically rather than by running it. Nothing
here needs a database, a bucket or a credential: it parses the modules
and compares the attribute names they read off `settings` against the
real dataclass. That catches the entire class of typo, on every entry
point, including ones added later - which a test asserting
`db_reader_username` specifically would not.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import fields
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config.settings import Settings  # noqa: E402


def _valid_settings_attributes() -> set[str]:
    """Field names plus the public methods callers legitimately use."""
    names = {f.name for f in fields(Settings)}
    names |= {n for n in dir(Settings) if not n.startswith("_")}
    return names


def _modules_using_platform_settings() -> list[Path]:
    """Every module that imports the platform Settings dataclass.

    Discovered rather than listed, so an entry point added next year is
    covered without anyone remembering to add it here. Modules that bind
    the name `settings` to something else entirely - core/web/app.py uses
    it for AuthSettings, core/assistant/* for AssistantSettings - do not
    import this class and so are not scanned.
    """
    found = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "from core.config.settings import" not in text:
            continue
        # The substring above is only a fast pre-filter: modules that
        # import env_var() alone (core/web/auth.py, core/web/app.py) bind
        # the name `settings` to AuthSettings, not this dataclass, so
        # scanning them checks the wrong contract. Only a module that
        # imports the Settings class itself is scanned.
        tree = ast.parse(text, filename=str(path))
        imports_settings_class = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "core.config.settings"
            and any(alias.name == "Settings" for alias in node.names)
            for node in ast.walk(tree)
        )
        if imports_settings_class:
            found.append(path)
    return found


def _settings_attributes_read(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "settings"
    }


def test_the_scan_actually_finds_the_entry_points():
    """A discovery test that silently matched nothing would pass forever."""
    scanned = {p.name for p in _modules_using_platform_settings()}
    assert {"__main__.py", "purge.py"} & scanned
    assert len(_modules_using_platform_settings()) >= 5


@pytest.mark.parametrize(
    "path", _modules_using_platform_settings(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_every_settings_attribute_read_exists(path):
    valid = _valid_settings_attributes()
    used = _settings_attributes_read(path)
    unknown = sorted(used - valid)
    assert not unknown, (
        f"{path.relative_to(ROOT)} reads {unknown} off Settings, which has no such "
        f"field. This is the bug class that made `python -m core.web` unstartable: "
        f"the attribute is only resolved at runtime, so a typo here is invisible "
        f"until someone runs the command. Available: {sorted(f.name for f in fields(Settings))}"
    )


def test_db_username_specifically_is_gone():
    """The original bug, pinned so it cannot come back by copy-paste."""
    for path in _modules_using_platform_settings():
        # Matched as a CALL, not a bare substring: the fix's own
        # explanatory comments name the old field, and a test that cannot
        # tell an explanation from a use is a test that punishes writing
        # the explanation down.
        assert "connect(settings, settings.db_username)" not in path.read_text(), (
            f"{path.relative_to(ROOT)} still connects as settings.db_username. "
            "Use db_reader_username to read, or db_ingest_username where the "
            "caller writes index_state - they hold different Postgres grants."
        )


def test_the_web_entry_point_connects_as_the_reader_role():
    """The web interface must not hold write grants on the index.

    Asserted on the source rather than by connecting: the point is which
    role name is passed, and that is decidable statically.
    """
    text = (ROOT / "core" / "web" / "__main__.py").read_text()
    assert "connect(settings, settings.db_reader_username)" in text
    assert "connect(settings, settings.db_ingest_username)" not in text, (
        "the web interface should never connect as the ingest role - it would gain "
        "INSERT on stored_resources, which core/db/bootstrap_aws.sql withholds "
        "from everything that serves users."
    )


def test_verification_connects_as_the_ingest_role():
    """Verification WRITES its audit checkpoint, so the reader is wrong.

    core/audit/checkpoint.py stores progress via write_index_state(),
    which INSERTs into index_state. The reader role holds only SELECT
    there, so connecting as the reader verifies correctly and then fails
    to save - silently reverting every run to re-reading the whole chain.
    """
    text = (ROOT / "core" / "verify" / "__main__.py").read_text()
    assert "connect(settings, settings.db_ingest_username)" in text


def test_roi_requests_is_granted_in_every_cloud_bootstrap():
    """The table existed with no grants at all, on any cloud.

    Fixing the username alone would have moved the failure from startup
    to the first time an HIM user opened the release-of-information page,
    which is strictly worse.
    """
    for cloud, reader in (
        ("aws", "phi_ai_reader"),
        ("azure", "phi_ai_reader"),
        ("gcp", "{READER_IAM_USER}"),
    ):
        sql = (ROOT / "core" / "db" / f"bootstrap_{cloud}.sql").read_text()
        assert f"GRANT SELECT, INSERT, UPDATE ON roi_requests TO {reader};" in sql, cloud
        assert f"ON SEQUENCE roi_requests_id_seq TO {reader};" in sql, cloud


def test_no_role_may_delete_a_disclosure_record():
    """45 CFR 164.528: the fulfilled rows ARE the accounting of
    disclosures. A row that can be deleted is not an accounting, so the
    absence of a DELETE grant is a control and needs a test saying so."""
    for cloud in ("aws", "gcp", "azure"):
        sql = (ROOT / "core" / "db" / f"bootstrap_{cloud}.sql").read_text()
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("GRANT") and "roi_requests" in stripped:
                assert "DELETE" not in stripped.split(" ON ")[0], (
                    f"bootstrap_{cloud}.sql grants DELETE on roi_requests: {stripped}"
                )


# ---------------------------------------------------------------------------
# Actually execute the wiring
# ---------------------------------------------------------------------------
#
# The static scan above catches attribute typos. It does NOT catch a call
# passing an argument a constructor does not accept - which is how
# EnvelopeEncryptor(kms=..., key_id=...) survived in SIX places, including
# one written while fixing the settings.db_username bug, copied from the
# broken lines around it. That is the failure mode of a wiring function
# nothing ever runs.
#
# So this runs build() for real, with only the boundaries stubbed: object
# storage, KMS, the audit sink and the database connector. Everything
# between them - constructor signatures, keyword names, argument order,
# attribute reads - executes exactly as it would in production.


class _StubKMS:
    def generate_data_key(self):
        return b"\x00" * 32, "d3Jhc="

    def decrypt_data_key(self, wrapped_dek_b64):
        return b"\x00" * 32


class _StubStorage:
    def iter_keys(self, prefix=""):
        return iter(())


class _StubAuditSink:
    def __call__(self, event):
        pass

    def last_hash(self):
        return "0" * 64

    def iter_events(self):
        return iter(())

    def read_all(self):
        return []


@pytest.fixture
def stub_infrastructure(monkeypatch, tmp_path):
    """Stub only the edges: cloud storage, KMS, audit sink, Postgres."""
    key = tmp_path / "epic.pem"
    # Deliberately NOT shaped like a PEM. Settings.from_env() only reads
    # the bytes; writing a realistic-looking key header here would trip
    # repository secret scanners for no benefit.
    key.write_bytes(b"not-a-key: this file exists only so the path check passes")

    for name, value in {
        "PHI_AI_CLOUD_PROVIDER": "aws",
        "PHI_AI_STORAGE_BUCKET": "records-bucket",
        "PHI_AI_STORAGE_REGION": "us-east-1",
        "PHI_AI_KMS_KEY_ID": "alias/records",
        "PHI_AI_AUDIT_BUCKET": "audit-bucket",
        "PHI_AI_AUDIT_KMS_KEY_ID": "alias/audit",
        "PHI_AI_FHIR_BASE_URL": "https://ehr.example/api/FHIR/R4",
        "PHI_AI_FHIR_TOKEN_URL": "https://ehr.example/oauth2/token",
        "PHI_AI_FHIR_CLIENT_ID": "client-id",
        "PHI_AI_FHIR_PRIVATE_KEY_PATH": str(key),
        "PHI_AI_DB_NAME": "phi_ai",
        "PHI_AI_DB_HOST": "db.example",
        "PHI_AI_DB_READER_USERNAME": "phi_ai_reader",
        "PHI_AI_DB_INGEST_USERNAME": "phi_ai_ingest",
        "PHI_AI_WEB_TRUST_PROXY_AUTH": "false",
        "PHI_AI_WEB_DEV_IDENTITY": "tester:him",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PHI_AI_ASSISTANT_ENABLED", raising=False)
    monkeypatch.delenv("PHI_AI_SMART_ISSUERS_PATH", raising=False)

    import core.storage.factory as factory

    monkeypatch.setattr(factory, "build_storage", lambda s: _StubStorage())
    monkeypatch.setattr(factory, "build_kms", lambda s: _StubKMS())
    monkeypatch.setattr(factory, "build_audit_sink", lambda s: _StubAuditSink())

    import core.db.connection as connection

    def _refuse(*a, **k):
        raise AssertionError("build() must not open a database connection eagerly")

    monkeypatch.setattr(connection, "connect", _refuse)
    return monkeypatch


def test_the_web_entry_point_wires_up(stub_infrastructure):
    """`python -m core.web` gets a working app out of build().

    This is the test that was missing. It fails on an attribute typo, a
    bad keyword, or a changed constructor signature anywhere in the
    wiring - the three separate bugs found here would all have been
    caught by it.
    """
    from core.web.__main__ import build

    app = build()

    assert app.state.reader is not None
    assert app.state.audit is not None
    assert app.state.roi is not None, "release of information should be wired up"
    assert getattr(app.state, "assistant", None) is None, "assistant is off by default"


def test_the_web_entry_point_says_which_variable_is_missing(stub_infrastructure):
    """A missing reader username is a named error, not an AttributeError.

    The match= below pins the exact variable name core/web/__main__.py
    puts in its RuntimeError text. It has to track the env-var rename in
    lockstep with that module - an operator who is told to set a variable
    that no longer exists is worse off than one given an AttributeError,
    because the wrong name looks authoritative.
    """
    stub_infrastructure.delenv("PHI_AI_DB_READER_USERNAME")

    from core.web.__main__ import build

    with pytest.raises(RuntimeError, match="PHI_AI_DB_READER_USERNAME"):
        build()


def test_build_opens_no_database_connection(stub_infrastructure):
    """Connections are lazy, so the app starts before Postgres is reachable.

    The stub raises if `connect` is called during build(); reaching the
    assertions in the wiring test above proves it was not.
    """
    from core.web.__main__ import build

    build()  # the monkeypatched connect() would fail this if called


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_dicom_grants_are_conditional(cloud):
    """DICOM imaging is optional and off by default, and every bootstrap
    file sets `\\set ON_ERROR_STOP on`.

    The grants were unconditional under a comment that said "only granted
    if you ran imaging_schema.sql", so the DEFAULT deployment - no
    imaging - ran every real grant, then hit "relation dicom_studies does
    not exist", halted, and exited non-zero. Nothing had actually failed
    and nothing was missing, but the setup runbook tells the operator that
    stopping early means something did, and a bootstrap that reports
    failure stops a deployment just as effectively as one that fails.

    Verified against real PostgreSQL 16 in both directions: skipped with a
    NOTICE when the imaging schema is absent, applied when it is present.
    """
    sql = (ROOT / "core" / "db" / f"bootstrap_{cloud}.sql").read_text()
    assert "to_regclass('public.dicom_studies')" in sql, (
        "DICOM grants must be guarded - imaging is optional"
    )
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("GRANT") and "dicom_" in stripped:
            pytest.fail(f"unconditional DICOM grant in bootstrap_{cloud}.sql: {stripped}")
# Made by Ryan Gomez & Co. Inc.
