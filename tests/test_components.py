# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The platform half of the Components screen: the registry's members, the
readers, the build stamp, the manifest, the migration ledger, the
five-step journal, the updater and the workstation CLI.

No database anywhere: the ledger and the journal are exercised with fake
psycopg-shaped connections (the way tests/test_assistant_ops.py fakes
one), the updater with a fake runner, the readers on tmp_path repos. The
one thing read from the real repository is the truth the screen is built
on: the PHP mirror's vocabulary, RELEASE, the CHANGELOG, the tags and
VENDORED.json.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from dataclasses import replace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
from core.components import build, ledger, manifest, members, vendored  # noqa: E402
from core.components import updater as upd  # noqa: E402
from core.components.journal import (  # noqa: E402
    ACTION_ACK, ACTION_STEP, JobOpen, Journal, apply_operator_config, missing_config_keys, retire_model,
)
from core.components.registry import (  # noqa: E402
    CADENCE_DAYS, GROUPS, KEEP, STEPS, Context, Fact, Reading, decide_state, load_members, read_all,
)

CLI = ROOT / "scripts" / "components.py"
MEMBERS = load_members()
PLATFORM_KEYS = [k for k, c in MEMBERS.items() if not c.demo_only]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    """Answers the ledger's queries from the FakeConn's declared objects."""

    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.conn.executed.append((flat, params))
        low = flat.lower()
        if "information_schema.tables" in low:
            self._rows = list(self.conn.tables)
        elif "information_schema.schemata" in low:
            self._rows = [(s,) for s in self.conn.schemas]
        elif "pg_roles" in low:
            self._rows = [(r,) for r in self.conn.roles]
        elif low.startswith("select") and "schema_migrations" in low:
            if not self.conn.ledger_exists:
                raise RuntimeError('relation "schema_migrations" does not exist')
            self._rows = list(self.conn.ledger_rows)
        elif low.startswith("insert into schema_migrations"):
            self.conn.ledger_rows.append((params[0], params[1], "2026-09-08", params[2], params[3]))
        elif low.startswith("create table"):
            self.conn.ledger_exists = True
        elif "vocab.concept" in low:
            self._rows = list(self.conn.vocab)
        elif low.startswith("select version()"):
            self._rows = [("PostgreSQL 16.4 (fake)",)]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, tables=(), schemas=("public",), roles=(), ledger_exists=False, ledger_rows=(), vocab=()):
        self.tables = list(tables)
        self.schemas = list(schemas)
        self.roles = list(roles)
        self.ledger_exists = ledger_exists
        self.ledger_rows = list(ledger_rows)
        self.vocab = list(vocab)
        self.executed = []
        self.committed = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


class TableStore:
    """A tiny keyed store behind a psycopg-shaped connection, so two Journal
    instances over the same 'database' see each other's rows."""

    KEYS = {"platform_updates": lambda p: p[0], "platform_update_steps": lambda p: (p[0], p[1]),
            "platform_component_acks": lambda p: p[0], "platform_backups": lambda p: p[0],
            "platform_releases": lambda p: p[0]}

    def __init__(self):
        self.tables = {name: {} for name in self.KEYS}
        self.executed = []

    def connect(self):
        store = self

        class Cur:
            def __init__(self):
                self._rows = []

            def execute(self, sql, params=None):
                flat = " ".join(sql.split())
                store.executed.append(flat)
                m = re.match(r"INSERT INTO (\w+)", flat)
                if m and m.group(1) in store.tables:
                    store.tables[m.group(1)][store.KEYS[m.group(1)](params)] = tuple(params)
                    return
                m = re.match(r"DELETE FROM (\w+) WHERE id = %s", flat)
                if m:
                    store.tables[m.group(1)].pop(params[0], None)
                    return
                m = re.match(r"SELECT .* FROM (\w+)", flat)
                if m and m.group(1) in store.tables:
                    self._rows = list(store.tables[m.group(1)].values())
                    return
                self._rows = []

            def fetchall(self):
                return list(self._rows)

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cur()

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        return Conn()


class FakeRunner:
    """A subprocess.run stand-in: records argv, fails what it is told to."""

    def __init__(self, fail_on=(), fail_once=()):
        self.calls = []
        self.fail_on = set(fail_on)
        self.fail_once = set(fail_once)

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        rc = 0
        for needle in list(self.fail_once):
            if needle in joined:
                rc = 1
                self.fail_once.discard(needle)
        if any(needle in joined for needle in self.fail_on):
            rc = 1
        return SimpleNamespace(returncode=rc, stdout="ok" if rc == 0 else "", stderr="" if rc == 0 else "boom")


class FakeUpdater:
    """The updater's surface as the journal calls it, without docker."""

    def __init__(self, previous="repo@sha256:old", verify_fails=0):
        self.previous = previous
        self.verify_fails = verify_fails
        self.calls = []

    def current_digest(self):
        return self.previous

    def pull(self, digest):
        self.calls.append(("pull", digest))
        return {"pulled": digest}

    def up(self, digest):
        self.calls.append(("up", digest))
        self.previous, prev = digest, self.previous
        return {"digest": digest, "previous_digest": prev}

    def healthcheck(self):
        self.calls.append(("healthcheck",))
        if self.verify_fails > 0:
            self.verify_fails -= 1
            raise upd.UpdaterError("healthcheck exited 1")
        return {"green": True}

    def rollback(self, previous):
        self.calls.append(("rollback", previous))
        return self.up(previous)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> str:
    """git against `root`, with git's own context variables stripped.

    THIS IS NOT OPTIONAL AND IT IS NOT DEFENSIVE. `-C` is a chdir, not a
    scope: GIT_DIR overrides it. scripts/pre_push_gates.sh runs this suite
    from a pre-push hook, where git exports GIT_DIR - so without the
    scrub, _git_repo() below builds its fixture and commits "one" onto the
    branch being pushed, moving HEAD to a three-file tree in the middle of
    the push that is verifying it.
    """
    run = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                         text=True, check=False, env=build.git_env())
    assert run.returncode == 0, f"git {' '.join(args)}: {run.stderr}"
    return run.stdout.strip()


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.email=t@example.org", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@example.org", "-c", "user.name=t", "commit", "-q", "-m", "one")
    return root


def _manifest(**entries) -> dict:
    return manifest.build({k: v for k, v in entries.items()}, produced_by="test", host="test", at=datetime.now(timezone.utc))


def _online(value: str, **extra) -> dict:
    return manifest.entry(value, "test: online", offline=False, **extra)


def _offline(value: str) -> dict:
    return manifest.entry(value, "test: offline", offline=True)


def _reading(key: str, ctx: Context) -> Reading:
    return MEMBERS[key].read(ctx)


def test_members_are_declared_in_group_order():
    groups = [c.group for c in MEMBERS.values()]
    assert groups == sorted(groups)


@pytest.mark.parametrize("key", list(MEMBERS))
def test_every_procedure_is_the_five_steps_in_its_mode(key):
    comp = MEMBERS[key]
    steps = comp.steps(Context(root=ROOT))
    assert tuple(s.name for s in steps) == STEPS
    assert all(s.verify for s in steps), f"{key}: a step without a verify text"
    if comp.mode == "record":
        assert not any(s.direct_capable for s in steps)
        assert not any(s.instruction for s in steps), f"{key}: a record row has nothing to run"
    elif comp.mode == "guided":
        assert not any(s.direct_capable for s in steps)
        assert all(s.instruction for s in steps), f"{key}: every guided step carries a copyable instruction"
    else:
        assert all(s.direct_capable for s in steps)
    assert all(s.runbook for s in steps), f"{key}: every step names its runbook"


def test_a_record_component_cannot_declare_a_direct_step():
    from core.components.registry import Component, Step
    comp = Component(key="x", group="A", name="x", kind="x", mode="record", backup_unit="", recovery="",
                     cadence_days=None, reader=lambda ctx: None,
                     procedure=lambda ctx: tuple(Step(n, direct_capable=(n == "Apply")) for n in STEPS))
    with pytest.raises(ValueError, match="record-only"):
        comp.steps(Context(root=ROOT))


# ---------------------------------------------------------------------------
# decide_state and the Fact/Reading rules
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _f(value, known=True):
    return Fact(value=value, source="t", checked_at=NOW if known else None)


@pytest.mark.parametrize("running,built,latest,expected", [
    (_f("1"), _f("1"), _f("1"), "current"),
    (_f("1"), _f("1"), _f("2"), "behind"),
    (_f("2"), _f("1"), _f("1"), "drifted"),
    (_f("1", known=False), _f("1"), _f("1"), "unknown"),
    (_f("1"), _f("1", known=False), _f("1"), "unknown"),
    (_f("1"), _f("1"), _f("1", known=False), "unknown"),
    (_f("2"), _f("1"), _f("1", known=False), "drifted"),
    (_f("unknown"), _f("unknown"), _f("unknown"), "unknown"),
])
def test_decide_state_cases(running, built, latest, expected):
    assert decide_state(running, built, latest) == expected


def test_decide_state_updating_beats_everything_and_newer_decides_behind():
    assert decide_state(_f("1", False), _f("1"), _f("1"), updating=True) == "updating"
    assert decide_state(_f("1"), _f("1"), _f("x"), newer=lambda latest, built: False) == "current"
    assert decide_state(_f("1"), _f("1"), _f("x"), newer=lambda latest, built: True) == "behind"


def test_a_fact_never_checked_is_unknown_whatever_it_says():
    assert not Fact(value="1.1.0", source="t").known
    assert not Fact.unknown("t", "why").known
    assert Fact(value="1.1.0", source="t", checked_at=NOW).known


def test_a_reading_refuses_a_state_outside_the_five():
    with pytest.raises(ValueError):
        Reading(key="x", running=_f("1"), built=_f("1"), latest=_f("1"), state="green")


# ---------------------------------------------------------------------------
# The build stamp and the manifest
# ---------------------------------------------------------------------------

def test_write_stamp_and_read_stamp_round_trip_in_a_checkout(tmp_path):
    root = _git_repo(tmp_path, {"RELEASE": "9.9.9\n", "a.txt": "a\n"})
    stamp = build.write_stamp(root, image_digest="repo@sha256:abc")
    back = build.read_stamp(root)
    assert back == stamp
    assert set(back) == {"release", "commit", "branch", "built_at", "tree_sha", "image_digest"}
    assert back["release"] == "9.9.9" and back["branch"] == "main"
    assert back["commit"] == _git(root, "rev-parse", "HEAD")
    assert back["tree_sha"] == _git(root, "rev-parse", "HEAD^{tree}")
    assert datetime.fromisoformat(back["built_at"]).tzinfo is not None


def test_runtime_stamp_derives_from_git_or_says_unknown(tmp_path):
    root = _git_repo(tmp_path, {"RELEASE": "9.9.9\n"})
    stamp, source = build.runtime_stamp(root)
    assert source.startswith("git") and stamp["commit"] == _git(root, "rev-parse", "HEAD")
    bare = tmp_path / "bare"
    bare.mkdir()
    stamp, source = build.runtime_stamp(bare)
    assert stamp["commit"] == "unknown" and stamp["tree_sha"] == "unknown" and "no BUILD.json" in source
    assert build.read_stamp(bare) is None
    (bare / "BUILD.json").write_text("not json")
    assert build.read_stamp(bare) is None


def test_manifest_load_age_and_stale(tmp_path):
    assert manifest.load(tmp_path) is None
    at = datetime.now(timezone.utc)
    man = manifest.build({"runtimes": manifest.entry("python:3.12-slim", "Dockerfile", offline=True)},
                         produced_by="t", host="h", at=at - timedelta(days=3))
    manifest.write(tmp_path, man)
    loaded = manifest.load(tmp_path)
    assert loaded["components"]["runtimes"]["offline"] is True
    assert 2 <= manifest.age(loaded, at).days <= 3
    assert not manifest.stale(loaded, at=at)
    assert manifest.stale(loaded, at=at + timedelta(days=CADENCE_DAYS["advisories"] + 1))
    assert manifest.stale(None)
    assert manifest.stale({"components": {}}), "an undated manifest is stale"


# ---------------------------------------------------------------------------
# Readers: never raise, honest on an empty root, right on a populated one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_every_reader_survives_an_empty_root_and_reads_unknown(key, tmp_path, monkeypatch):
    monkeypatch.delenv("PHI_AI_IMAGE", raising=False)
    monkeypatch.delenv("PHI_AI_CLOUD_PROVIDER", raising=False)
    reading = _reading(key, Context(root=tmp_path))
    assert reading.key == key
    assert reading.state in ("unknown", "drifted"), f"{key} read {reading.state} from an empty directory"
    for fact in (reading.running, reading.built, reading.latest):
        assert fact.source, f"{key}: a fact without a source"


def test_a_reader_that_raises_yields_an_unknown_reading_that_says_why():
    from core.components.registry import Component
    comp = Component(key="boom", group="A", name="x", kind="x", mode="record", backup_unit="", recovery="",
                     cadence_days=None, reader=lambda ctx: 1 / 0, procedure=lambda ctx: ())
    r = comp.read(Context(root=ROOT))
    assert r.state == "unknown" and "ZeroDivisionError" in r.note


def test_release_reader_is_current_when_the_three_agree_and_says_when_not(tmp_path):
    root = _git_repo(tmp_path, {"RELEASE": core.__version__ + "\n",
                                "CHANGELOG.md": f"# Changelog\n\n## {core.__version__} — 2026-09-08: x\n"})
    _git(root, "tag", core.__version__)
    r = _reading("release", Context(root=root))
    assert r.state == "current" and r.note == ""
    assert ("RELEASE", core.__version__, "RELEASE") in r.evidence.rows
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 9.9.9 — 2026-09-09: newer\n")
    assert _reading("release", Context(root=root)).state == "behind"
    (root / "RELEASE").write_text("0.0.1\n")
    r = _reading("release", Context(root=root))
    assert r.state == "drifted" and "no tag equals 0.0.1" in r.note


def test_build_stamp_reader(tmp_path):
    root = _git_repo(tmp_path, {"RELEASE": "9.9.9\n"})
    assert _reading("build_stamp", Context(root=root)).state == "unknown"
    stamp = build.write_stamp(root)
    value = f"9.9.9 @ {stamp['commit'][:12]}"
    ctx = Context(root=root, build=build.read_stamp(root), manifest=_manifest(build_stamp=_offline(value)))
    r = _reading("build_stamp", ctx)
    assert r.state == "current", (r.running, r.built, r.latest)
    assert r.built.value == value and r.running.value == value
    ctx = Context(root=root, build=build.read_stamp(root), manifest=_manifest(build_stamp=_offline("9.9.9 @ ffffffffffff")))
    assert _reading("build_stamp", ctx).state == "behind"
    (root / "RELEASE").write_text("9.9.10\n")
    assert _reading("build_stamp", Context(root=root, build=build.read_stamp(root))).state == "drifted"


def test_running_image_reader(tmp_path, monkeypatch):
    root = tmp_path
    (root / "docker-compose.yml").write_text("services:\n  web:\n    build:\n      context: .\n    restart: unless-stopped\n")
    monkeypatch.delenv("PHI_AI_IMAGE", raising=False)
    r = _reading("running_image", Context(root=root))
    assert r.state == "unknown" and "not pinned" in r.running.value and "unknown without a stamp" in r.built.value
    assert ("web", "built from Dockerfile", "unless-stopped", "always") in r.evidence.rows
    digest = "repo@sha256:" + "a" * 64
    monkeypatch.setenv("PHI_AI_IMAGE", digest)
    stamp = {"release": "9.9.9", "commit": "c", "branch": "b", "built_at": "t", "tree_sha": "t", "image_digest": digest}
    ctx = Context(root=root, build=stamp, manifest=_manifest(running_image=_offline(digest)))
    assert _reading("running_image", ctx).state == "current"
    monkeypatch.setenv("PHI_AI_IMAGE", "repo@sha256:" + "b" * 64)
    assert _reading("running_image", ctx).state == "drifted"
    stamp_no_digest = {k: v for k, v in stamp.items() if k != "image_digest"}
    r = _reading("running_image", Context(root=root, build=stamp_no_digest))
    assert r.state == "unknown" and "no image_digest" in r.built.value
    # the release unit: image: ${PHI_AI_IMAGE:-tag} on a built service is read
    # as "pinned by PHI_AI_IMAGE, default tag", and an unset variable names the tag
    (root / "docker-compose.yml").write_text("services:\n  web:\n    image: ${PHI_AI_IMAGE:-phi-ai:dev}\n"
                                             "    build:\n      context: .\n      dockerfile: Dockerfile\n"
                                             "    restart: unless-stopped\n  viewer:\n    image: ohif/app:v3.13.4\n")
    monkeypatch.delenv("PHI_AI_IMAGE", raising=False)
    r = _reading("running_image", Context(root=root))
    assert r.state == "unknown" and "default tag phi-ai:dev" in r.running.value
    assert ("web", "PHI_AI_IMAGE, default phi-ai:dev (build: Dockerfile)", "unless-stopped", "always") in r.evidence.rows
    assert ("viewer", "ohif/app:v3.13.4", "no", "always") in r.evidence.rows
    assert members._compose_default_image("services: {}") == ""


def test_python_pins_reader(tmp_path):
    import importlib.metadata
    pytest_v = importlib.metadata.version("pytest")
    yaml_v = importlib.metadata.version("PyYAML")
    (tmp_path / "requirements.lock").write_text(f"pytest=={pytest_v} \\\n    --hash=sha256:x\npyyaml=={yaml_v}\n")
    r = _reading("python_pins", Context(root=tmp_path))
    assert r.built.value == "2 pins as locked" and r.running.value == r.built.value
    assert r.state == "unknown" and "no manifest" in r.latest.value
    r = _reading("python_pins", Context(root=tmp_path, manifest=_manifest(python_pins=_offline("x"))))
    assert r.state == "unknown" and "produced offline" in r.latest.value
    lock_sha = vendored.sha256_file(tmp_path / "requirements.lock")
    ok = _manifest(python_pins=_online("no advisory", advisories=[], lock_sha256=lock_sha))
    assert _reading("python_pins", Context(root=tmp_path, manifest=ok)).state == "current"
    bad = _manifest(python_pins=_online("1 advisory", advisories=["CVE-x"], lock_sha256=lock_sha))
    assert _reading("python_pins", Context(root=tmp_path, manifest=bad)).state == "behind"
    (tmp_path / "requirements.lock").write_text(f"pytest=={pytest_v}\nno-such-dist-phi==1.0\npyyaml==0.0.1\n")
    r = _reading("python_pins", Context(root=tmp_path, manifest=ok))
    assert r.state == "drifted" and "1 of 3 pins differ" in r.running.value and "1 not installed" in r.running.value
    assert ("no-such-dist-phi", "1.0", "absent", "not installed") in r.evidence.rows


def test_runtimes_reader(tmp_path):
    mm = ".".join(sys.version_info[:2].__str__().strip("()").split(", "))
    (tmp_path / "Dockerfile").write_text(f"FROM python:{mm}-slim\n")
    r = _reading("runtimes", Context(root=tmp_path, manifest=_manifest(runtimes=_online(f"python {mm}"))))
    assert r.state == "current" and r.running.value == f"python {mm}"
    assert any(row[0] == "index server" and "no index connection" in row[1] for row in r.evidence.rows)
    r = _reading("runtimes", Context(root=tmp_path, connect=lambda: FakeConn()))
    assert ("index server", "PostgreSQL 16.4 (fake)", "SELECT version()") in r.evidence.rows
    (tmp_path / "Dockerfile").write_text("FROM python:2.7-slim\n")
    assert _reading("runtimes", Context(root=tmp_path, manifest=_manifest(runtimes=_online("x")))).state == "drifted"


def test_vendored_scan_records_banners_and_never_invents_a_font_version(tmp_path):
    assets = tmp_path / "site" / "public" / "assets"
    (assets / "fonts").mkdir(parents=True)
    (assets / "d3.v7.min.js").write_text("// https://d3js.org v7.9.0 Copyright\n!function(){}();\n")
    (assets / "app.js").write_text("/* Copyright 2026 Ryan Gomez & Co. Inc. */\nconsole.log(1);\n")
    (assets / "third.js").write_text("/* some library without a version */\n")
    (assets / "fonts" / "inter-400-latin.woff2").write_bytes(b"\x00font")
    (assets / "fonts" / "README.md").write_text("Licensed under the **SIL OFL 1.1**.\n- https://fonts.example/inter\n")
    rows = vendored.scan(tmp_path)
    by = {r["path"]: r for r in rows}
    assert set(by) == {"site/public/assets/d3.v7.min.js", "site/public/assets/fonts/inter-400-latin.woff2",
                       "site/public/assets/fonts/README.md"}
    assert by["site/public/assets/d3.v7.min.js"]["version"] == "7.9.0"
    assert by["site/public/assets/fonts/inter-400-latin.woff2"]["version"] == "unrecorded"
    assert by["site/public/assets/fonts/inter-400-latin.woff2"]["family"] == "Inter"
    assert by["site/public/assets/fonts/README.md"]["upstream"] == ["https://fonts.example/inter"]
    assert by["site/public/assets/fonts/README.md"]["license"] == "SIL OFL 1.1"
    vendored.write(tmp_path)
    r = _reading("vendored_frontend", Context(root=tmp_path, manifest=_manifest(vendored_frontend=_online("x"))))
    assert r.state == "current" and r.built.value == "3 files as recorded"
    moved = _manifest(vendored_frontend=_online("d3 7.9.1 upstream", behind=["site/public/assets/d3.v7.min.js"]))
    assert _reading("vendored_frontend", Context(root=tmp_path, manifest=moved)).state == "behind"
    (assets / "d3.v7.min.js").write_text("// https://d3js.org v7.9.1\n")
    r = _reading("vendored_frontend", Context(root=tmp_path, manifest=_manifest(vendored_frontend=_online("x"))))
    assert r.state == "drifted" and "1 of 3 files differ" in r.running.value
    (assets / "d3.v7.min.js").unlink()
    r = _reading("vendored_frontend", Context(root=tmp_path, manifest=_manifest(vendored_frontend=_online("x"))))
    assert r.state == "current", "a file absent from this tree is not drift"
    assert "1 recorded files are not in this tree" in r.note


def test_the_committed_vendored_json_is_what_a_fresh_scan_produces():
    recorded = vendored.load(ROOT)
    assert recorded is not None, "VENDORED.json is missing; run scripts/components.py vendored"
    assert recorded["files"] == vendored.scan(ROOT)
    paths = {f["path"] for f in recorded["files"]}
    assert "core/web/static/fonts/README.md" in paths, "the platform's own vendored typefaces are recorded"
    assert paths == {f["path"] for f in vendored.scan(ROOT)}, "the record names exactly what a scan of this tree finds"
    assert all(f["version"] == "unrecorded" for f in recorded["files"] if f["kind"] in ("font", "fonts README"))


def test_release_file_is_the_one_source_of_the_release():
    release = (ROOT / "RELEASE").read_text(encoding="utf-8").strip()
    assert release and re.fullmatch(r"\d+\.\d+\.\d+(-[\w.]+)?", release), f"RELEASE reads {release!r}"
    assert core.__version__ == release, "core.__version__ must read RELEASE"
    heads = [ln for ln in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines() if ln.startswith("## ")]
    assert heads[0].split()[1] == release, f"CHANGELOG's top heading {heads[0]!r} does not name RELEASE {release}"
    tags = _git(ROOT, "tag").split()
    major_minor = ".".join(release.split(".")[:2])
    assert release in tags or major_minor in tags, f"no tag equals {release} or {major_minor}; tags: {tags}"


def test_the_fallback_literal_equals_the_release_file():
    src = (ROOT / "core" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.M)
    assert m and m.group(1) == (ROOT / "RELEASE").read_text().strip(), "the fallback literal must equal RELEASE"


# ---------------------------------------------------------------------------
# The registry's vocabulary and members equal the PHP mirror's
# ---------------------------------------------------------------------------


def test_read_all_reads_only_the_platforms_rows():
    keys = [c.key for c, _ in read_all(Context(root=ROOT))]
    assert keys == PLATFORM_KEYS and len(keys) == 18
    assert all(not MEMBERS[k].demo_only for k in keys)


def test_infra_pins_reader(tmp_path):
    aws = tmp_path / "deploy" / "aws"
    aws.mkdir(parents=True)
    aws.joinpath("versions.tf").write_text(
        'terraform {\n  required_version = ">= 1.10.0"\n  required_providers {\n    aws = {\n      source  = "hashicorp/aws"\n'
        '      version = "~> 5.40"\n    }\n  }\n}\nprovider "aws" {\n  default_tags {\n    tags = {\n      x = "y"\n    }\n  }\n}\n')
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    online = _manifest(infra_pins=_online("terraform >= 1.10.0; aws ~> 5.40; base python:3.12-slim"))
    r = _reading("infra_pins", Context(root=tmp_path, manifest=online))
    assert r.state == "unknown" and "no .terraform.lock.hcl" in r.running.value
    aws.joinpath(".terraform.lock.hcl").write_text(
        'provider "registry.terraform.io/hashicorp/aws" {\n  version     = "5.100.0"\n  constraints = "~> 5.40"\n}\n')
    r = _reading("infra_pins", Context(root=tmp_path, manifest=online))
    assert r.state == "current", (r.running, r.built, r.latest)
    assert ("selected aws", "5.100.0 (satisfies ~> 5.40)", "deploy/aws/.terraform.lock.hcl") in r.evidence.rows
    aws.joinpath(".terraform.lock.hcl").write_text(
        'provider "registry.terraform.io/hashicorp/aws" {\n  version     = "6.0.0"\n  constraints = "~> 5.40"\n}\n')
    r = _reading("infra_pins", Context(root=tmp_path, manifest=online))
    assert r.state == "drifted" and "aws 6.0.0 outside ~> 5.40" in r.running.value
    assert members._satisfies("1.10.0", ">= 1.10.0") and not members._satisfies("1.9.9", ">= 1.10.0")
    assert members._satisfies("3.6.2", "~> 3.6") and not members._satisfies("4.0.0", "~> 3.6")


def test_emr_profiles_reader_names_the_vendors_nobody_has_checked():
    """REPLACES test_emr_profiles_reader_records_no_date_it_does_not_have.

    That test asserted every profile read "not recorded" and that the
    reading was "unknown" - which was true, and was the problem. It did not
    describe a behaviour; it described a gap, and pinned it. Filling in a
    single vendor's doc_checked date would have turned it red, so the test
    stood guard over the emptiness it was reporting.

    What the reading owes an operator is not a blanket "unknown" but WHICH
    vendors nobody has verified, so the gap is a work list rather than a
    mood. That is what this asserts.
    """
    r = _reading("emr_profiles", Context(root=ROOT))
    assert r.running.value == r.built.value and r.built.value.endswith("vendor profiles")
    assert r.evidence.columns[-2:] == ("Doc checked", "Source")

    from core.fhir.emr_profiles import PROFILES

    dated = {k for k, p in PROFILES.items() if p.doc_checked}
    undated = set(PROFILES) - dated

    if undated:
        assert r.state == "unknown"
        for key in undated:
            assert key in r.latest.value, (
                f"{key} has no doc_checked date and the reading does not name it - "
                "an operator cannot act on a count"
            )
    else:
        assert r.state != "unknown"


def test_a_profile_records_the_page_that_was_read_or_no_date_at_all():
    """A date with no source is somebody's memory of having checked.

    Both fields or neither - and the reader reports a half-recorded check
    as an error rather than counting it, because half a check reads like a
    check on the screen.
    """
    from core.fhir.emr_profiles import PROFILES

    for key, p in PROFILES.items():
        assert bool(p.doc_checked) == bool(p.doc_source), (
            f"{key}: doc_checked={p.doc_checked!r} doc_source={p.doc_source!r} - "
            "record the date AND the page it was read from, or neither"
        )
        if p.doc_checked:
            datetime.strptime(p.doc_checked, "%Y-%m-%d")   # ISO, or this raises
            assert p.doc_source.startswith("https://"), key


def test_a_stale_check_is_not_treated_as_a_fresh_one():
    """A date is not the same as a recent date.

    The reading used to count dated profiles and stop, so a check from
    three years ago and one from this morning were the same fact. Here
    every profile carries a date older than the cadence, and the reading
    still has to refuse to go green.
    """
    from core.components.registry import CADENCE_DAYS
    from core.fhir import emr_profiles

    cadence = CADENCE_DAYS["vendor_docs"]
    old = (datetime.now(timezone.utc).date() - timedelta(days=cadence + 30)).isoformat()
    patched = {
        k: replace(p, doc_checked=old, doc_source="https://example.invalid/docs")
        for k, p in emr_profiles.PROFILES.items()
    }
    with mock.patch.object(emr_profiles, "PROFILES", patched):
        r = _reading("emr_profiles", Context(root=ROOT))
    assert r.state == "unknown", "a check older than the cadence must not read as current"
    assert "more than" in r.latest.value and str(cadence) in r.latest.value


def test_terminology_reader_says_release_ids_are_not_recorded():
    r = _reading("terminology", Context(root=ROOT))
    assert r.state == "unknown" and "not recorded" in r.running.value and "no index connection" in r.running.value
    conn = FakeConn(vocab=[("LOINC", 5), ("SNOMED", 7)])
    r = _reading("terminology", Context(root=ROOT, connect=lambda: conn, manifest=_manifest(terminology=_online("x"))))
    assert r.state == "unknown" and "12 concepts in 2 vocabularies" in r.running.value
    assert ("vocab.concept LOINC", "", "", "not recorded", "5") in r.evidence.rows


def test_model_catalogue_reader_and_retirement():
    from core.web.platform_state import PlatformState
    ps = PlatformState()
    online = _manifest(model_catalogue=_online("no registered id retired", deprecated=[]))
    r = _reading("model_catalogue", Context(root=ROOT, platform_state=ps, manifest=online))
    assert r.state == "current", (r.running, r.built)
    assert r.evidence.columns[-1] == "Retired upstream"
    default = members._default_model(ROOT)
    retired = _manifest(model_catalogue=_online("1 registered id retired", deprecated=[default]))
    r = _reading("model_catalogue", Context(root=ROOT, platform_state=ps, manifest=retired))
    assert r.state == "behind" and any(row[3] == default and row[5] == "retired upstream" for row in r.evidence.rows)
    assert _reading("model_catalogue", Context(root=ROOT, manifest=online)).state == "unknown"
    default = members._default_model(ROOT)
    assert default, "core/assistant/config.py names no DEFAULT_MODEL"
    out = retire_model(ps, default, "admin")
    assert out["rows"] and all(p["previous_status"] == "enabled" for p in out["rows"])
    r = _reading("model_catalogue", Context(root=ROOT, platform_state=ps, manifest=online))
    assert r.state == "drifted" and default in r.running.value
    with pytest.raises(ValueError):
        retire_model(ps, "no-such-model", "admin")


def test_synthetic_corpus_reader(tmp_path):
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    man = {"generator": "synthea", "generator_version": "v4.0.0", "seed": "20260821", "bundles": 33,
           "resources_marked": 33564, "jar_sha256": "ed43", "generated_at": "2026-08-26T17:01:21+00:00"}
    (tmp_path / "tests" / "fixtures" / "layer2.MANIFEST.json").write_text(json.dumps(man))
    (tmp_path / "scripts" / "generate_corpus.py").write_text('SYNTHEA_VERSION = "v4.0.0"\nSEED = "20260821"\n')
    r = _reading("synthetic_corpus", Context(root=tmp_path))
    assert r.state == "current" and r.running.value == "synthea v4.0.0 seed 20260821"
    (tmp_path / "scripts" / "generate_corpus.py").write_text('SYNTHEA_VERSION = "v4.1.0"\nSEED = "20260821"\n')
    assert _reading("synthetic_corpus", Context(root=tmp_path)).state == "drifted"
    (tmp_path / "scripts" / "generate_corpus.py").write_text("x = 1\n")
    r = _reading("synthetic_corpus", Context(root=tmp_path))
    assert r.state == "unknown" and "declares no SYNTHEA_VERSION" in r.running.value


def _ledger_conn_for(root: Path, applied: list[str], extra_tables=()) -> FakeConn:
    migs = {m["name"]: m for m in ledger.list_migrations(root)}
    tables, schemas, roles = set(extra_tables), {"public"}, set()
    rows = []
    for name in applied:
        m = migs[name]
        tables.update(m["tables"])
        schemas.update(m["schemas"])
        roles.update(m["roles"])
        rows.append((name, m["sha256"], "2026-09-01", "ops", "by hand"))
    return FakeConn(tables=sorted(tables), schemas=sorted(schemas), roles=sorted(roles), ledger_exists=True,
                    ledger_rows=rows)


def test_migration_ledger_reader(tmp_path, monkeypatch):
    db = tmp_path / "core" / "db"
    db.mkdir(parents=True)
    db.joinpath("schema.sql").write_text("CREATE TABLE IF NOT EXISTS stored_resources (id INT);\n")
    db.joinpath("schema_partitioned.sql").write_text("CREATE TABLE IF NOT EXISTS stored_resources (id INT) PARTITION BY LIST (t);\n")
    db.joinpath("identity_schema.sql").write_text("CREATE SCHEMA IF NOT EXISTS identity;\nCREATE TABLE identity.patient_identity (id INT);\n")
    db.joinpath("bootstrap_gcp.sql").write_text("CREATE ROLE phi_ai_reader WITH LOGIN;\n")
    db.joinpath("bootstrap_aws.sql").write_text("CREATE ROLE phi_ai_reader WITH LOGIN;\n")
    monkeypatch.setenv("PHI_AI_CLOUD_PROVIDER", "aws")
    r = _reading("migration_ledger", Context(root=tmp_path))
    assert r.state == "unknown" and "no index connection" in r.running.value
    assert r.built.value == "4 of 4 schema files applied with matching checksums", r.built.value
    conn = _ledger_conn_for(tmp_path, ["schema.sql", "identity_schema.sql", "bootstrap_aws.sql"])
    r = _reading("migration_ledger", Context(root=tmp_path, connect=lambda: conn))
    assert r.state == "current", (r.running, r.built, r.evidence.rows)
    states = {row[0]: row[3] for row in r.evidence.rows}
    assert states["schema_partitioned.sql"] == "alternative of schema.sql"
    assert states["bootstrap_gcp.sql"].startswith("for another cloud")
    conn = _ledger_conn_for(tmp_path, ["schema.sql"])
    r = _reading("migration_ledger", Context(root=tmp_path, connect=lambda: conn))
    assert r.state == "drifted" and r.running.value.startswith("2 of 4")
    conn.ledger_rows[0] = ("schema.sql", "0" * 64, "2026-09-01", "ops", "by hand")
    r = _reading("migration_ledger", Context(root=tmp_path, connect=lambda: conn))
    assert {row[0]: row[3] for row in r.evidence.rows}["schema.sql"] == "checksum differs"


def test_key_ages_reader_reads_ages_only(tmp_path):
    key = tmp_path / "epic_private_key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n")
    (tmp_path / "epic_public_key.pem").write_text("pub\n")
    r = _reading("key_ages", Context(root=tmp_path))
    assert r.state == "current" and r.running.value.startswith("2 key files; oldest 0 days")
    assert not any("not a real key" in cell for row in r.evidence.rows for cell in row)
    old = (datetime.now(timezone.utc) - timedelta(days=CADENCE_DAYS["keys"] + 5)).timestamp()
    os.utime(key, (old, old))
    r = _reading("key_ages", Context(root=tmp_path))
    assert r.state == "behind" and "rotation was due" in r.note
    assert any(row[0] == "epic_private_key.pem" and row[-1] == "overdue" for row in r.evidence.rows)


def test_operator_config_reader_and_direct_apply(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "thing.example.yaml").write_text("alpha: 1\nbeta:\n  gamma: 2\n  delta: 3\n")
    r = _reading("operator_config", Context(root=tmp_path))
    assert r.state == "current" and "0 operator files present of 1" in r.running.value
    (cfg / "thing.yaml").write_text("# my comment\nalpha: 5\nbeta:\n  gamma: 9\n")
    r = _reading("operator_config", Context(root=tmp_path))
    assert r.state == "behind" and "1 keys missing" in r.running.value
    assert ("thing.example.yaml", "thing.yaml", "alpha, beta", "beta.delta") in r.evidence.rows
    assert missing_config_keys({"a": 1, "b": {"c": 2}}, {"b": {}}) == ["a", "b.c"]
    (cfg / "thing.example.yaml").write_text("alpha: 1\nbeta:\n  gamma: 2\nepsilon: [1, 2]\n")
    out = apply_operator_config(tmp_path, "thing.yaml")
    assert out["added"] == ["epsilon"] and out["previous"] == "config/thing.yaml.previous"
    text = (cfg / "thing.yaml").read_text()
    assert text.startswith("# my comment\n") and yaml.safe_load(text) == {"alpha": 5, "beta": {"gamma": 9}, "epsilon": [1, 2]}
    assert (cfg / "thing.yaml.previous").read_text() == "# my comment\nalpha: 5\nbeta:\n  gamma: 9\n"
    assert _reading("operator_config", Context(root=tmp_path)).state == "current"


def test_last_green_gates_reader(tmp_path):
    root = _git_repo(tmp_path, {"RELEASE": "9.9.9\n", "scripts/pre_push_gates.sh": 'echo "pre-push gate 1/1: x"\n'})
    r = _reading("last_green_gates", Context(root=root))
    assert r.state == "unknown" and "no .gates/last_green.json" in r.built.value
    assert ("pre-push gate 1/1: x", "declared by the script", "scripts/pre_push_gates.sh") in r.evidence.rows
    head = _git(root, "rev-parse", "HEAD")
    (root / ".gates").mkdir()
    (root / ".gates" / "last_green.json").write_text(json.dumps({"commit": head, "at": "2026-09-08T10:00:00+00:00", "gates": ["x"]}))
    assert _reading("last_green_gates", Context(root=root)).state == "current"
    (root / ".gates" / "last_green.json").write_text(json.dumps({"commit": "f" * 40, "at": "t"}))
    assert _reading("last_green_gates", Context(root=root)).state == "drifted"


def test_group_e_readers_read_the_journal(tmp_path):
    j = Journal(root=tmp_path)
    ctx = Context(root=tmp_path, journal=j)
    assert _reading("backups", ctx).state == "unknown"
    assert _reading("releases_kept", ctx).state == "unknown"
    assert _reading("update_journal", ctx).state == "unknown"
    j.record_backup("Operational state (platform_* tables)", "s3://store/system/backups/1.dump", "ab" * 32, "updater", True)
    r = _reading("backups", ctx)
    assert r.state == "behind" and "rehearsal never recorded" in r.latest.value
    j.rehearsed("Operational state (platform_* tables)", "admin")
    r = _reading("backups", ctx)
    assert r.state == "current" and r.latest.value.startswith("rehearsal due ")
    assert r.evidence.rows[0][4] == "verified by the updater"
    j.record_release("repo@sha256:" + "1" * 64, "1.1.0", {"commit": "abc123def456789"})
    r = _reading("releases_kept", ctx)
    assert r.state == "current" and r.running.value.startswith("1 of 1 digests kept")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "x.example.yaml").write_text("a: 1\n")
    job = j.start_job("operator_config", "x.yaml", "admin", "guided")
    r = _reading("update_journal", ctx)
    assert r.state == "behind" and "a job is open" in r.note
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin", attested=True)
    j.advance(job["id"], "Apply", "ok", "admin", attested=True)
    j.advance(job["id"], "Verify", "ok", "admin", attested=True)
    j.finish(job["id"], "admin")
    r = _reading("update_journal", ctx)
    assert r.state == "current" and "done; 4 of 5 steps done" in r.running.value
    assert any(row[0] == "Back up" and row[5] == "attested" for row in r.evidence.rows)


def test_context_helper_loads_manifest_and_stamp_and_marks_the_open_job(tmp_path):
    build.write_stamp(tmp_path)
    manifest.write(tmp_path, _manifest())
    j = Journal(root=tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "x.example.yaml").write_text("a: 1\n")
    j.start_job("operator_config", "x.yaml", "admin", "guided")
    ctx = members.context(tmp_path, journal=j)
    assert ctx.build and ctx.manifest and ctx.open_job_key == "operator_config"
    r = MEMBERS["operator_config"].read(ctx)
    assert r.state == "updating"


# ---------------------------------------------------------------------------
# The migration ledger
# ---------------------------------------------------------------------------

def test_objects_in_parses_tables_schemas_and_roles_and_ignores_comments():
    sql = ("-- CREATE TABLE commented_out (x INT);\nCREATE SCHEMA IF NOT EXISTS vocab;\n"
           "CREATE TABLE IF NOT EXISTS vocab.concept (\n  id INT\n);\nCREATE TABLE plain (x INT);\n"
           "create role omop_etl WITH LOGIN;\nCREATE INDEX IF NOT EXISTS idx ON plain (x);\nCREATE OR REPLACE VIEW v AS SELECT 1;\n")
    got = ledger.objects_in(sql)
    assert got == {"tables": [("vocab", "concept"), ("public", "plain")], "schemas": ["vocab"], "roles": ["omop_etl"]}


def test_list_migrations_globs_every_sql_file_with_its_sha256():
    rows = ledger.list_migrations(ROOT)
    names = [r["name"] for r in rows]
    assert names == sorted(p.name for p in (ROOT / "core" / "db").glob("*.sql")) and len(names) >= 27
    assert "components_schema.sql" in names
    comp = next(r for r in rows if r["name"] == "components_schema.sql")
    assert ("public", "schema_migrations") in comp["tables"] and ("public", "platform_updates") in comp["tables"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", r["sha256"]) for r in rows)


def test_backfill_writes_rows_for_present_files_after_a_dump(tmp_path):
    db = tmp_path / "core" / "db"
    db.mkdir(parents=True)
    db.joinpath("components_schema.sql").write_text((ROOT / "core" / "db" / "components_schema.sql").read_text())
    db.joinpath("schema.sql").write_text("CREATE TABLE IF NOT EXISTS stored_resources (id INT);\n")
    db.joinpath("identity_schema.sql").write_text("CREATE SCHEMA IF NOT EXISTS identity;\nCREATE TABLE identity.patient_identity (id INT);\n")
    db.joinpath("imaging_schema.sql").write_text("CREATE TABLE dicom_studies (id INT);\nCREATE TABLE dicom_series (id INT);\n")
    db.joinpath("bootstrap_azure.sql").write_text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO x;\n")
    db.joinpath("prompts_schema.sql").write_text("CREATE TABLE assistant_prompts (id INT);\n")
    conn = FakeConn(tables=[("public", "stored_resources"), ("identity", "patient_identity"), ("public", "dicom_studies"),
                            ("public", "schema_migrations"), ("public", "platform_updates"), ("public", "platform_update_steps"),
                            ("public", "platform_backups"), ("public", "platform_component_acks"), ("public", "platform_releases")],
                    schemas=["public", "identity"], ledger_exists=True,
                    ledger_rows=[("schema.sql", "x", "2026-09-01", "ops", "by hand")])
    runner = FakeRunner()
    report = ledger.backfill(conn, "ryan", root=tmp_path, dsn="postgresql://index/phi", runner=runner,
                             which=lambda name: "/usr/bin/pg_dump", dump_dir=tmp_path / "dumps",
                             at=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc))
    assert report["backfilled"] == ["components_schema.sql", "identity_schema.sql"]
    assert report["already_in_ledger"] == ["schema.sql"]
    assert report["partial"] == ["imaging_schema.sql"] and report["absent"] == ["prompts_schema.sql"]
    assert report["unverifiable"] == ["bootstrap_azure.sql"]
    assert report["schemas"] == ["identity", "public"]
    dump = runner.calls[0]
    assert dump[0] == "/usr/bin/pg_dump" and "--format=custom" in dump and dump.count("--schema") == 2
    assert "--dbname=postgresql://index/phi" in dump and dump[2].endswith("ledger-backfill-20260908T120000Z.dump")
    inserts = [(sql, params) for sql, params in conn.executed if sql.startswith("INSERT INTO schema_migrations")]
    assert [p[0] for _, p in inserts] == ["components_schema.sql", "identity_schema.sql"]
    assert inserts[1][1][2] == "ryan" and inserts[1][1][3].startswith("backfill; dump ")
    assert inserts[0][1][3] == "applied by the ledger backfill run"
    assert inserts[1][1][1] == vendored.sha256_file(db / "identity_schema.sql")
    assert conn.committed >= 1
    assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in sql for sql, _ in conn.executed)


def test_backfill_refuses_without_pg_dump_unless_attested(tmp_path):
    db = tmp_path / "core" / "db"
    db.mkdir(parents=True)
    db.joinpath("components_schema.sql").write_text((ROOT / "core" / "db" / "components_schema.sql").read_text())
    db.joinpath("schema.sql").write_text("CREATE TABLE stored_resources (id INT);\n")
    conn = FakeConn(tables=[("public", "stored_resources")], ledger_exists=True)
    with pytest.raises(ledger.LedgerError, match="pg_dump"):
        ledger.backfill(conn, "ryan", root=tmp_path, dsn="postgresql://x", which=lambda name: None, runner=FakeRunner())
    assert not [1 for sql, _ in conn.executed if sql.startswith("INSERT INTO schema_migrations")]
    report = ledger.backfill(conn, "ryan", root=tmp_path, dsn="postgresql://x", which=lambda name: None,
                             runner=FakeRunner(), attest_no_dump=True)
    assert report["attested_no_dump"] and report["backfilled"] == ["schema.sql"] and report["dump"] is None
    note = [p for sql, p in conn.executed if sql.startswith("INSERT INTO schema_migrations")][0][3]
    assert note == "backfill; attested: no dump taken by ryan"
    failing = FakeRunner(fail_on=("pg_dump",))
    conn2 = FakeConn(tables=[("public", "stored_resources")], ledger_exists=True)
    with pytest.raises(ledger.LedgerError, match="pg_dump failed"):
        ledger.backfill(conn2, "ryan", root=tmp_path, dsn="postgresql://x", which=lambda name: "pg_dump", runner=failing)


def test_ledger_status_and_applied_without_a_ledger_table(tmp_path):
    db = tmp_path / "core" / "db"
    db.mkdir(parents=True)
    db.joinpath("schema.sql").write_text("CREATE TABLE stored_resources (id INT);\n")
    assert ledger.applied(FakeConn(ledger_exists=False)) == []
    rows = ledger.status(tmp_path, None)
    assert rows[0]["ledger"] == "unknown"
    rows = ledger.status(tmp_path, FakeConn(ledger_exists=True))
    assert rows[0]["ledger"] == "not in the ledger"


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

def _guided_job(tmp_path, key="operator_config", target="x.yaml"):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "x.example.yaml").write_text("a: 1\n")
    j = Journal(root=tmp_path)
    job = j.start_job(key, target, "admin", "guided")
    return j, job


def test_start_job_refuses_record_components_and_bad_modes(tmp_path):
    j = Journal(root=tmp_path)
    with pytest.raises(ValueError, match="record-only"):
        j.start_job("release", "1.1.0", "admin", "guided")
    with pytest.raises(ValueError, match="cannot run direct"):
        j.start_job("infra_pins", "x", "admin", "direct")
    with pytest.raises(ValueError, match="unknown component"):
        j.start_job("nope", "x", "admin", "guided")
    with pytest.raises(ValueError, match="mode"):
        j.start_job("running_image", "x", "admin", "record")
    with pytest.raises(ValueError, match="target"):
        j.start_job("running_image", "  ", "admin", "guided")
    assert j.open_job() is None and j.events == []


def test_the_open_job_is_the_lock(tmp_path):
    j, job = _guided_job(tmp_path)
    assert j.open_job()["id"] == job["id"]
    with pytest.raises(JobOpen, match=job["id"]):
        j.start_job("key_ages", "epic", "someone", "guided")
    assert job["status"] == "planned" and [s["status"] for s in job["steps"]] == ["pending"] * 5
    assert job["plan"]["phrase"] == "operator_config x.yaml" and len(job["plan"]["steps"]) == 5
    assert j.events == [(ACTION_STEP, f"components/operator_config/{job['id']}/Plan", "admin")]


def test_the_plan_phrase_must_name_the_component_and_the_target(tmp_path):
    j, job = _guided_job(tmp_path)
    with pytest.raises(ValueError, match="operator_config x.yaml"):
        j.confirm_plan(job["id"], "yes", "admin")
    with pytest.raises(ValueError, match="confirm the plan"):
        j.advance(job["id"], "Back up", "ok", "admin")
    job = j.confirm_plan(job["id"], " operator_config x.yaml ", "admin")
    assert job["status"] == "confirmed" and job["steps"][0]["status"] == "done"
    with pytest.raises(ValueError, match="already confirmed"):
        j.confirm_plan(job["id"], "operator_config x.yaml", "admin")


def test_back_up_cannot_be_skipped_and_steps_run_in_order(tmp_path):
    j, job = _guided_job(tmp_path)
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    with pytest.raises(ValueError, match="Back up may not be skipped"):
        j.advance(job["id"], "Back up", "skipped", "admin")
    with pytest.raises(ValueError, match="not the step waiting"):
        j.advance(job["id"], "Apply", "ok", "admin")
    with pytest.raises(ValueError, match="Plan is confirmed"):
        j.advance(job["id"], "Plan", "ok", "admin")
    with pytest.raises(ValueError):
        j.advance(job["id"], "Back up", "maybe", "admin")
    job = j.advance(job["id"], "Back up", "ok", "admin", attested=True, evidence={"snapshot": "rds-1"})
    assert job["status"] == "running" and job["steps"][1]["attested"] and job["steps"][1]["evidence"] == {"snapshot": "rds-1"}
    with pytest.raises(ValueError, match="nothing to recover"):
        j.advance(job["id"], "Recover", "ok", "admin")
    job = j.advance(job["id"], "Apply", "ok", "admin", attested=True)
    with pytest.raises(ValueError, match="attestation"):
        j.advance(job["id"], "Verify", "skipped", "admin")
    job = j.advance(job["id"], "Verify", "ok", "admin", attested=True)
    assert job["steps"][4]["status"] == "skipped" and job["steps"][4]["actor"] == "engine"
    assert j.next_step(job["id"]) is None
    job = j.finish(job["id"], "admin")
    assert job["status"] == "done" and job["finished_at"] and j.open_job() is None
    with pytest.raises(ValueError, match="finished"):
        j.advance(job["id"], "Verify", "ok", "admin")
    assert j.history("operator_config")[0]["id"] == job["id"]
    assert j.events[-1] == (ACTION_STEP, f"components/operator_config/{job['id']}/done", "admin")


def test_a_failed_back_up_fails_closed(tmp_path):
    j, job = _guided_job(tmp_path)
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    job = j.advance(job["id"], "Back up", "failed", "admin", evidence={"error": "no space"})
    assert job["status"] == "failed"
    with pytest.raises(ValueError, match="finish it"):
        j.advance(job["id"], "Apply", "ok", "admin")
    job = j.finish(job["id"], "admin")
    assert job["status"] == "failed" and j.open_job() is None


def test_verify_failed_moves_the_job_to_recover(tmp_path):
    j, job = _guided_job(tmp_path)
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin", attested=True)
    j.advance(job["id"], "Apply", "ok", "admin", attested=True)
    job = j.advance(job["id"], "Verify", "failed", "admin", evidence={"healthcheck": "FAIL"})
    assert job["status"] == "verify_failed" and j.next_step(job["id"]) == "Recover"
    with pytest.raises(ValueError, match="recover"):
        j.finish(job["id"], "admin")
    job = j.advance(job["id"], "Recover", "ok", "admin", attested=True)
    assert job["status"] == "recovered"
    job = j.finish(job["id"], "admin")
    assert job["status"] == "recovered" and j.open_job() is None
    assert j.history("operator_config")[0]["status"] == "recovered"


def test_apply_failed_skips_verify_and_recover_failed_fails(tmp_path):
    j, job = _guided_job(tmp_path)
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin", attested=True)
    job = j.advance(job["id"], "Apply", "failed", "admin")
    assert job["status"] == "recovering" and job["steps"][3]["status"] == "skipped" and j.next_step(job["id"]) == "Recover"
    job = j.advance(job["id"], "Recover", "failed", "admin")
    assert job["status"] == "failed"
    assert j.finish(job["id"], "admin")["status"] == "failed"


def test_rollback_guided_waits_on_the_admin_and_before_apply_needs_nothing(tmp_path):
    j, job = _guided_job(tmp_path)
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin", attested=True)
    job = j.rollback(job["id"], "admin")
    assert job["status"] == "recovered" and job["steps"][2]["status"] == "skipped"
    assert job["steps"][4]["evidence"]["note"].startswith("nothing was applied")
    j.finish(job["id"], "admin")
    job = j.start_job("operator_config", "x.yaml", "admin", "guided")
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin", attested=True)
    j.advance(job["id"], "Apply", "ok", "admin", attested=True)
    job = j.rollback(job["id"], "admin")
    assert job["status"] == "recovering" and job["steps"][3]["status"] == "skipped" and j.next_step(job["id"]) == "Recover"
    job = j.advance(job["id"], "Recover", "ok", "admin", attested=True)
    assert job["status"] == "recovered"
    with pytest.raises(ValueError, match="nothing to roll back"):
        j.rollback(job["id"], "admin")


def test_finish_abandons_a_job_that_never_applied(tmp_path):
    j, job = _guided_job(tmp_path)
    job = j.finish(job["id"], "admin")
    assert job["status"] == "failed" and all(s["status"] == "skipped" for s in job["steps"])
    assert j.open_job() is None


def test_direct_operator_config_job_applies_verifies_and_recovers(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "x.example.yaml").write_text("a: 1\nb: 2\n")
    (cfg / "x.yaml").write_text("a: 5\n")
    j = Journal(root=tmp_path)
    job = j.start_job("operator_config", "x.yaml", "admin", "direct")
    assert job["mode"] == "direct" and job["plan"]["steps"][2]["direct_capable"]
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    job = j.advance(job["id"], "Back up", "ok", "admin")
    assert job["previous"]["previous"] == "config/x.yaml.previous" and job["steps"][1]["evidence"]["sha256"]
    job = j.advance(job["id"], "Apply", "ok", "admin")
    assert job["steps"][2]["outcome"] == "ok" and job["steps"][2]["evidence"]["added"] == ["b"]
    assert yaml.safe_load((cfg / "x.yaml").read_text()) == {"a": 5, "b": 2}
    job = j.advance(job["id"], "Verify", "ok", "admin")
    assert job["steps"][3]["outcome"] == "ok" and job["status"] == "running"
    assert j.finish(job["id"], "admin")["status"] == "done"
    job = j.start_job("operator_config", "x.yaml", "admin", "direct")
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin")
    (cfg / "x.example.yaml").write_text("a: 1\nb: 2\nc: 3\n")
    j.advance(job["id"], "Apply", "ok", "admin")
    assert yaml.safe_load((cfg / "x.yaml").read_text()) == {"a": 5, "b": 2, "c": 3}
    job = j.rollback(job["id"], "admin")
    assert job["status"] == "recovered", job["steps"][4]
    assert yaml.safe_load((cfg / "x.yaml").read_text()) == {"a": 5, "b": 2}
    assert job["steps"][4]["evidence"]["verify_again"]["previous_is_back"] is True
    (cfg / "x.yaml").write_text("tampered: 1\n")
    j.finish(job["id"], "admin")
    job = j.start_job("operator_config", "x.yaml", "admin", "direct")
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin")
    j.advance(job["id"], "Apply", "ok", "admin")
    (cfg / "x.yaml.previous").write_text("someone: moved it\n")
    job = j.rollback(job["id"], "admin")
    assert job["status"] == "failed" and "not the previous file" in job["steps"][4]["evidence"]["verify_again"]["error"]


def test_direct_apply_that_raises_records_a_failed_outcome(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "x.example.yaml").write_text("a: 1\n")
    (cfg / "x.yaml").write_text("- not a mapping\n")
    j = Journal(root=tmp_path)
    job = j.start_job("operator_config", "x.yaml", "admin", "direct")
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin")
    job = j.advance(job["id"], "Apply", "ok", "admin")
    assert job["steps"][2]["outcome"] == "failed" and "ValueError" in job["steps"][2]["evidence"]["error"]
    assert job["status"] == "recovering"


def test_direct_model_retirement_and_recovery():
    from core.web.platform_state import PlatformState
    ps = PlatformState()
    default = members._default_model(ROOT)
    j = Journal(root=ROOT, platform_state=ps)
    job = j.start_job("model_catalogue", default, "admin", "direct")
    j.confirm_plan(job["id"], f"model_catalogue {default}", "admin")
    job = j.advance(job["id"], "Back up", "ok", "admin")
    assert job["previous"]["rows"][0]["previous_status"] == "enabled"
    job = j.advance(job["id"], "Apply", "ok", "admin")
    assert all(m["status"] == "retired" for m in ps.models if m["model_id"] == default)
    job = j.advance(job["id"], "Verify", "ok", "admin")
    assert job["steps"][3]["outcome"] == "ok"
    job = j.rollback(job["id"], "admin")
    assert job["status"] == "recovered", job["steps"][4]
    assert job["steps"][4]["evidence"]["verify_again"]["restored"]
    assert all(m["status"] == "enabled" for m in ps.models if m["model_id"] == default)
    assert j.finish(job["id"], "admin")["status"] == "recovered"


def test_running_image_direct_needs_the_updater_service(tmp_path):
    j = Journal(root=tmp_path)
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    with pytest.raises(ValueError, match="updater service"):
        j.advance(job["id"], "Back up", "ok", "admin")
    job = j.advance(job["id"], "Back up", "ok", "admin", mode="guided", attested=True)
    assert job["steps"][1]["mode"] == "guided"
    j2 = Journal(root=tmp_path)
    t = j2.start_job("terminology", "2026-09", "admin", "direct")
    j2.confirm_plan(t["id"], "terminology 2026-09", "admin")
    with pytest.raises(ValueError, match="slice 3"):
        j2.advance(t["id"], "Back up", "ok", "admin")


def test_running_image_direct_job_runs_through_a_fake_updater(tmp_path):
    fake = FakeUpdater()
    j = Journal(root=tmp_path, updater=fake)
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    job = j.advance(job["id"], "Back up", "ok", "updater")
    assert job["previous"] == {"digest": "repo@sha256:old"}
    job = j.advance(job["id"], "Apply", "ok", "updater")
    assert fake.calls[:2] == [("pull", "repo@sha256:new"), ("up", "repo@sha256:new")]
    job = j.advance(job["id"], "Verify", "ok", "updater")
    assert job["status"] == "running" and j.finish(job["id"], "updater")["status"] == "done"
    fake = FakeUpdater(verify_fails=1)
    j = Journal(root=tmp_path, updater=fake)
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    j.advance(job["id"], "Back up", "ok", "updater")
    j.advance(job["id"], "Apply", "ok", "updater")
    job = j.advance(job["id"], "Verify", "ok", "updater")
    assert job["status"] == "verify_failed" and job["steps"][3]["outcome"] == "failed"
    job = j.rollback(job["id"], "updater")
    assert job["status"] == "recovered" and ("rollback", "repo@sha256:old") in fake.calls
    assert job["steps"][4]["evidence"]["verify_again"] == {"green": True}
    fake = FakeUpdater(previous=None)
    j = Journal(root=tmp_path, updater=fake)
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    job = j.advance(job["id"], "Back up", "ok", "updater")
    assert job["status"] == "failed" and "no previous digest" in job["steps"][1]["evidence"]["error"]


def test_acknowledgement_is_dated_reasoned_and_audited(tmp_path):
    j = Journal(root=tmp_path)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        j.acknowledge("python_pins", "next week", "because", "admin")
    with pytest.raises(ValueError, match="reason"):
        j.acknowledge("python_pins", "2026-12-31", "  ", "admin")
    with pytest.raises(ValueError, match="unknown component"):
        j.acknowledge("nope", "2026-12-31", "x", "admin")
    ack = j.acknowledge("python_pins", "2026-12-31", "the release lands next sprint", "admin")
    assert ack["component"] == "python_pins" and ack["until_date"] == "2026-12-31"
    assert j.acknowledgements("python_pins") == [ack] and j.acknowledgements("runtimes") == []
    assert j.acknowledged("python_pins", datetime(2026, 12, 31).date()) == ack
    assert j.acknowledged("python_pins", datetime(2027, 1, 1).date()) is None
    assert j.events == [(ACTION_ACK, "components/python_pins", "admin")]


def test_release_retention_keeps_three(tmp_path):
    clock = [datetime(2026, 9, 8, tzinfo=timezone.utc)]

    def tick():
        clock[0] += timedelta(minutes=1)
        return clock[0]

    j = Journal(root=tmp_path, clock=tick)
    for i in range(5):
        j.record_release(f"repo@sha256:{i}", f"1.1.{i}", {"commit": f"c{i}"})
    rows = j.releases()
    assert [r["digest"] for r in rows] == [f"repo@sha256:{i}" for i in (4, 3, 2, 1, 0)]
    assert [r["kept"] for r in rows] == [True, True, True, False, False]
    assert [r["digest"] for r in j.kept_releases()] == ["repo@sha256:4", "repo@sha256:3", "repo@sha256:2"]
    assert KEEP["images"] == 3
    with pytest.raises(ValueError):
        j.record_release("", "x", {})


def test_backup_retention_and_rehearsal(tmp_path):
    clock = [datetime(2026, 9, 8, tzinfo=timezone.utc)]

    def tick():
        clock[0] += timedelta(hours=1)
        return clock[0]

    j = Journal(root=tmp_path, clock=tick)
    for i in range(4):
        j.record_backup("Index (Postgres)", f"snap-{i}", "", "admin", verified=False)
    assert len(j.backups()) == 4, "attestations never prune: only a newer verified backup does"
    j.record_backup("Index (Postgres)", "dump-4", "ab" * 32, "updater", verified=True)
    rows = j.backups()
    assert len(rows) == KEEP["backups"] == 3 and rows[0]["location"] == "dump-4" and rows[0]["verified"]
    assert rows[0]["rehearsed_at"] == ""
    j.record_backup("config/", "cfg-1", "cd" * 32, "updater", verified=True)
    j.rehearsed("Index (Postgres)", "admin")
    latest = j.latest_backups()
    assert set(latest) == {"Index (Postgres)", "config/"}
    assert latest["Index (Postgres)"]["rehearsed_by"] == "admin" and latest["config/"]["rehearsed_at"] == ""
    with pytest.raises(ValueError):
        j.rehearsed("", "admin")


def test_events_are_audit_shaped_for_every_transition(tmp_path):
    j, job = _guided_job(tmp_path)
    j.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    j.advance(job["id"], "Back up", "ok", "admin", attested=True)
    j.rollback(job["id"], "admin")
    j.finish(job["id"], "admin")
    assert all(len(e) == 3 and e[0] == ACTION_STEP and e[1].startswith(f"components/operator_config/{job['id']}/") for e in j.events)
    assert [e[1].rsplit("/", 1)[1] for e in j.events] == ["Plan", "Plan", "Back up", "Recover", "recovered"]


def test_sql_write_through_and_the_lock_across_two_processes(tmp_path):
    store = TableStore()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "x.example.yaml").write_text("a: 1\n")
    web = Journal(store.connect, root=tmp_path)
    assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in sql for sql in store.executed)
    job = web.start_job("operator_config", "x.yaml", "admin", "guided")
    assert job["id"] in {k for k in store.tables["platform_updates"]}
    assert len(store.tables["platform_update_steps"]) == 5
    other = Journal(store.connect, root=tmp_path)
    assert other.open_job()["id"] == job["id"], "the second process sees the first's open job"
    with pytest.raises(JobOpen):
        other.start_job("key_ages", "k", "someone", "guided")
    web.confirm_plan(job["id"], "operator_config x.yaml", "admin")
    web.advance(job["id"], "Back up", "ok", "admin", attested=True)
    assert other.open_job()["status"] == "running" and other.open_job()["steps"][1]["attested"] is True
    web.rollback(job["id"], "admin")
    web.finish(job["id"], "admin")
    assert other.open_job() is None and other.history("operator_config")[0]["status"] == "recovered"
    web.acknowledge("python_pins", "2026-12-31", "x", "admin")
    web.record_backup("s", "loc", "c", "u", True)
    web.record_release("d", "1.1.0", {"commit": "c"})
    third = Journal(store.connect, root=tmp_path)
    assert third.acknowledgements("python_pins")[0]["reason"] == "x"
    assert third.backups()[0]["store"] == "s" and third.releases()[0]["digest"] == "d"


def test_a_failing_database_degrades_to_the_in_memory_journal(tmp_path):
    def boom():
        raise RuntimeError("db down")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "x.example.yaml").write_text("a: 1\n")
    j = Journal(boom, root=tmp_path)
    job = j.start_job("operator_config", "x.yaml", "admin", "guided")
    assert j.open_job()["id"] == job["id"]


# ---------------------------------------------------------------------------
# The updater
# ---------------------------------------------------------------------------

def _signed_release(tmp_path, digest, key_path, sig_key=None):
    rel = tmp_path / "release"
    rel.mkdir(exist_ok=True)
    stamp = {"release": "1.1.0", "commit": "c" * 40, "branch": "main", "built_at": "t", "tree_sha": "t", "image_digest": digest}
    (rel / "BUILD.json").write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n")
    sha = vendored.sha256_file(rel / "BUILD.json")
    (rel / "MANIFEST.sha256").write_text(f"{'a' * 64}  README.md\n{sha}  BUILD.json\n")
    upd.sign_file(rel / "MANIFEST.sha256", sig_key or key_path)
    return rel


def test_signature_round_trip_and_tamper(tmp_path):
    key = tmp_path / "keys" / "signing.key"
    pub = upd.keygen(key)
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600" and pub.startswith("-----BEGIN PUBLIC KEY-----")
    pub_path = tmp_path / "release_signing.pub"
    pub_path.write_text(pub)
    man = tmp_path / "MANIFEST.sha256"
    man.write_text("abc  file\n")
    sig = upd.sign_file(man, key)
    assert sig == tmp_path / "MANIFEST.sha256.sig" and len(sig.read_bytes()) == 64
    got = upd.verify_file(man, sig, pub_path)
    assert got["verified"] and got["sha256"] == vendored.sha256_file(man)
    man.write_text("abd  file\n")
    with pytest.raises(upd.SignatureError, match="altered"):
        upd.verify_file(man, sig, pub_path)
    man.write_text("abc  file\n")
    other = tmp_path / "other.key"
    (tmp_path / "other.pub").write_text(upd.keygen(other))
    with pytest.raises(upd.SignatureError):
        upd.verify_file(man, sig, tmp_path / "other.pub")
    with pytest.raises(upd.SignatureError, match="no signature"):
        upd.verify_file(man, tmp_path / "missing.sig", pub_path)


def test_the_trees_public_key_is_ed25519_and_no_private_key_is_in_the_tree():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not (ROOT / "config" / "release_signing.pub").is_file():
        pytest.skip("the verification key is placed on the host, not in the tree")
    pub = serialization.load_pem_public_key((ROOT / "config" / "release_signing.pub").read_bytes())
    assert isinstance(pub, Ed25519PublicKey)
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True, check=True).stdout.split("\0")
    for rel in tracked:
        p = ROOT / rel
        if rel and p.is_file() and p.suffix in (".pub", ".pem", ".key", ".json", ".py", ".yaml", ".yml", ".txt", ".md"):
            head = p.read_bytes()[:4096]
            assert b"BEGIN PRIVATE KEY" not in head or rel.startswith("tests/"), f"{rel} carries a private key"


def test_updater_pull_up_healthcheck_rollback_with_a_fake_runner(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PHI_AI_DB_NAME=phi\nPHI_AI_IMAGE=repo@sha256:old\n")
    runner = FakeRunner()
    u = upd.Updater(runner=runner, compose_file=tmp_path / "docker-compose.yml", env_file=env)
    assert u.current_digest() == "repo@sha256:old"
    assert u.pull("repo@sha256:new")["command"] == ["docker", "pull", "repo@sha256:new"]
    out = u.up("repo@sha256:new")
    assert out["previous_digest"] == "repo@sha256:old" and out["digest"] == "repo@sha256:new"
    assert env.read_text() == "PHI_AI_DB_NAME=phi\nPHI_AI_IMAGE=repo@sha256:new\n"
    assert runner.calls[-1] == ["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "--env-file", str(env),
                                "up", "-d", "--remove-orphans", "--no-build"], "a release is pulled, never built"
    assert u.healthcheck()["green"] is True
    assert runner.calls[-1][-4:] == ["web", "python", "-m", "core.healthcheck"] and "exec" in runner.calls[-1]
    back = u.rollback("repo@sha256:old")
    assert back["rolled_back_to"] == "repo@sha256:old" and u.current_digest() == "repo@sha256:old"
    with pytest.raises(upd.UpdaterError, match="no previous digest"):
        u.rollback("")
    env.write_text("OTHER=1\n")
    assert u.current_digest() is None
    u.write_digest("repo@sha256:x")
    assert env.read_text() == "OTHER=1\nPHI_AI_IMAGE=repo@sha256:x\n"


def test_updater_failures_raise_with_the_command_as_evidence(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PHI_AI_IMAGE=repo@sha256:old\n")
    u = upd.Updater(runner=FakeRunner(fail_on=("core.healthcheck",)), compose_file=tmp_path / "c.yml", env_file=env)
    with pytest.raises(upd.UpdaterError, match="exited 1"):
        u.healthcheck()
    u = upd.Updater(runner=FakeRunner(fail_on=("docker pull",)), compose_file=tmp_path / "c.yml", env_file=env)
    with pytest.raises(upd.UpdaterError, match="docker pull"):
        u.pull("repo@sha256:new")

    def missing(argv, **kw):
        raise FileNotFoundError("docker")

    u = upd.Updater(runner=missing, compose_file=tmp_path / "c.yml", env_file=env)
    with pytest.raises(upd.UpdaterError, match="not on PATH"):
        u.pull("x")


def test_prechecks_reject_an_unsigned_or_mismatched_release(tmp_path):
    key = tmp_path / "signing.key"
    pub = tmp_path / "release_signing.pub"
    pub.write_text(upd.keygen(key))
    u = upd.Updater(runner=FakeRunner(), compose_file=tmp_path / "c.yml", env_file=tmp_path / ".env", pubkey_path=pub)
    with pytest.raises(upd.SignatureError, match="no manifest"):
        u.prechecks("repo@sha256:new", tmp_path / "release")
    rel = _signed_release(tmp_path, "repo@sha256:new", key)
    got = u.prechecks("repo@sha256:new", rel)
    assert got["digest"] == "repo@sha256:new" and got["signature"]["verified"]
    with pytest.raises(upd.SignatureError, match="not the job's target"):
        u.prechecks("repo@sha256:other", rel)
    (rel / "BUILD.json").write_text(json.dumps({"image_digest": "repo@sha256:new", "release": "evil"}))
    with pytest.raises(upd.SignatureError, match="not the one the signed manifest lists"):
        u.prechecks("repo@sha256:new", rel)
    other = tmp_path / "other.key"
    upd.keygen(other)
    rel = _signed_release(tmp_path, "repo@sha256:new", key, sig_key=other)
    with pytest.raises(upd.SignatureError, match="another key"):
        u.prechecks("repo@sha256:new", rel)


def test_run_job_drives_the_five_steps_and_rolls_back_on_a_failed_verify(tmp_path):
    key = tmp_path / "signing.key"
    pub = tmp_path / "release_signing.pub"
    pub.write_text(upd.keygen(key))
    env = tmp_path / ".env"
    env.write_text("PHI_AI_IMAGE=repo@sha256:old\n")
    rel = _signed_release(tmp_path, "repo@sha256:new", key)
    runner = FakeRunner()
    u = upd.Updater(runner=runner, compose_file=tmp_path / "c.yml", env_file=env, pubkey_path=pub)
    j = Journal(root=tmp_path, updater=u)
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    final = upd.run_job(j, j.open_job(), u, rel)
    assert final["status"] == "done" and [s["status"] for s in final["steps"]] == ["done", "done", "done", "done", "skipped"]
    assert final["steps"][1]["evidence"]["digest"] == "repo@sha256:old"
    assert u.current_digest() == "repo@sha256:new" and j.open_job() is None
    assert [c[:2] for c in runner.calls] == [["docker", "pull"], ["docker", "compose"], ["docker", "compose"]]
    runner = FakeRunner(fail_once=("core.healthcheck",))
    u = upd.Updater(runner=runner, compose_file=tmp_path / "c.yml", env_file=env, pubkey_path=pub)
    j = Journal(root=tmp_path, updater=u)
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    final = upd.run_job(j, j.open_job(), u, rel)
    assert final["status"] == "recovered" and final["steps"][3]["outcome"] == "failed" and final["steps"][4]["outcome"] == "ok"
    assert u.current_digest() == "repo@sha256:new", "the env file was pinned to the previous digest... which was new"
    assert final["steps"][4]["evidence"]["recover"]["rolled_back_to"] == "repo@sha256:new"
    (rel / "MANIFEST.sha256.sig").write_bytes(b"\0" * 64)
    j = Journal(root=tmp_path, updater=u)
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    final = upd.run_job(j, j.open_job(), u, rel)
    assert final["status"] == "failed" and "precheck" in final["steps"][1]["evidence"]
    assert final["steps"][2]["status"] == "skipped" and "closed as failed" in final["steps"][2]["evidence"]["note"]
    assert not any(c[:2] == ["docker", "pull"] for c in runner.calls[3:]), "nothing was touched"


def test_updater_main_once_with_an_injected_journal(tmp_path, capsys):
    key = tmp_path / "signing.key"
    pub = tmp_path / "release_signing.pub"
    pub.write_text(upd.keygen(key))
    env = tmp_path / ".env"
    env.write_text("PHI_AI_IMAGE=repo@sha256:old\n")
    rel = _signed_release(tmp_path, "repo@sha256:new", key)
    u = upd.Updater(runner=FakeRunner(), compose_file=tmp_path / "c.yml", env_file=env, pubkey_path=pub)
    j = Journal(root=tmp_path, updater=u)
    assert upd.main(["--once", "--root", str(tmp_path), "--release-dir", str(rel)], journal=j, updater=u) == 0
    job = j.start_job("running_image", "repo@sha256:new", "admin", "direct")
    j.confirm_plan(job["id"], "running_image repo@sha256:new", "admin")
    assert upd.main(["--once", "--root", str(tmp_path), "--release-dir", str(rel)], journal=j, updater=u) == 0
    assert j.history("running_image")[0]["status"] == "done"
    assert upd.main(["--once", "--root", str(tmp_path)]) == 2, "no index database configured: the updater says so"
    assert "index database" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The workstation CLI
# ---------------------------------------------------------------------------

def _cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, cwd=cwd, timeout=300)


def test_cli_build_writes_a_stamp_that_read_stamp_reads_back_and_signs_it(tmp_path):
    root = _git_repo(tmp_path, {"RELEASE": "9.9.9\n", "README.md": "hi\n", "core/x.py": "x = 1\n"})
    key = tmp_path / "signing.key"
    (root / "config").mkdir()
    (root / "config" / "release_signing.pub").write_text(upd.keygen(key))
    run = _cli("--root", str(root), "build", "--image-digest", "repo@sha256:abc", "--sign", str(key))
    assert run.returncode == 0, run.stderr
    stamp = build.read_stamp(root)
    assert stamp["release"] == "9.9.9" and stamp["image_digest"] == "repo@sha256:abc" and stamp["commit"] == _git(root, "rev-parse", "HEAD")
    man = (root / "MANIFEST.sha256").read_text().splitlines()
    assert [ln.split("  ", 1)[1] for ln in man] == ["README.md", "RELEASE", "core/x.py", "BUILD.json"]
    assert man[0].startswith(vendored.sha256_file(root / "README.md"))
    upd.verify_file(root / "MANIFEST.sha256", root / "MANIFEST.sha256.sig", root / "config" / "release_signing.pub")
    run = _cli("--root", str(root), "verify")
    assert run.returncode == 0 and "verified" in run.stdout
    (root / "MANIFEST.sha256").write_text("tampered\n")
    assert _cli("--root", str(root), "verify").returncode == 1


def test_cli_check_offline_writes_a_manifest_marked_offline_and_online_is_not_faked(tmp_path):
    run = _cli("check")
    assert run.returncode == 2 and "NOT YET IMPLEMENTED" in run.stderr and "slice 4" in run.stderr
    out = tmp_path / "components.manifest.json"
    run = _cli("check", "--offline", "--out", str(out))
    assert run.returncode == 0, run.stderr
    man = json.loads(out.read_text())
    assert man["produced_at"] and man["produced_by"].startswith("scripts/components.py check --offline") and man["host"]
    comps = man["components"]
    for key in ("build_stamp", "python_pins", "runtimes", "infra_pins", "model_catalogue", "key_ages", "vendored_frontend"):
        assert key in comps, f"offline check wrote no {key}"
        assert comps[key]["offline"] is True and comps[key]["source"] and comps[key]["fetched_at"]
    assert comps["python_pins"]["advisories"] is None and comps["model_catalogue"]["deprecated"] is None
    assert "not checked" in comps["python_pins"]["value"]
    ctx = Context(root=ROOT, manifest=man)
    assert _reading("python_pins", ctx).latest.known is False, "an offline entry is never a latest known for advisories"
    assert "produced offline" in _reading("runtimes", ctx).latest.value


def test_cli_show_renders_every_platform_component(tmp_path):
    run = _cli("show", "--json")
    assert run.returncode == 0, run.stderr
    rows = json.loads(run.stdout)
    assert [r["key"] for r in rows] == PLATFORM_KEYS
    assert all(r["state"] in ("current", "behind", "drifted", "unknown", "updating") for r in rows)
    run = _cli("show")
    assert run.returncode == 0 and "A. Code and build" in run.stdout and "release" in run.stdout


def test_cli_keygen_refuses_a_path_inside_the_repository(tmp_path):
    run = _cli("keygen", "--out", str(ROOT / "config" / "oops.key"))
    assert run.returncode == 2 and "inside the repository" in run.stderr
    assert not (ROOT / "config" / "oops.key").exists()
    root = tmp_path / "repo"
    root.mkdir()
    run = _cli("--root", str(root), "keygen", "--out", str(tmp_path / "k.key"))
    assert run.returncode == 0 and (root / "config" / "release_signing.pub").read_text().startswith("-----BEGIN PUBLIC KEY-----")
    assert oct((tmp_path / "k.key").stat().st_mode & 0o777) == "0o600"


def test_cli_vendored_check_is_current():
    run = _cli("vendored", "--check")
    assert run.returncode == 0, run.stderr + run.stdout


# ---------------------------------------------------------------------------
# The deployment files
# ---------------------------------------------------------------------------

RELEASE_UNIT = "${PHI_AI_IMAGE:-phi-ai:dev}"


def test_compose_puts_the_release_unit_on_every_dockerfile_service_and_the_updater_under_its_profile():
    now = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    built = {name for name, svc in now["services"].items()
             if (svc.get("build") or {}).get("dockerfile") == "Dockerfile"}
    assert built == {"app", "web", "scheduler", "verify", "bulk-scheduler"}, built
    for name in built:
        assert now["services"][name]["image"] == RELEASE_UNIT, f"{name}: one image, pinned by PHI_AI_IMAGE"
    assert now["services"]["viewer"]["image"] == "ohif/app:v3.13.4", "the viewer keeps its own pin"
    assert "image" not in now["services"]["updater"], "the updater is built, never released through itself"
    assert any("releases:" in v and v.endswith(":ro") for v in now["services"]["updater"]["volumes"]), \
        "the release drop is releases/ (release/ collides with RELEASE on a case-insensitive filesystem)"
    assert "release/" not in (ROOT / ".gitignore").read_text() and "releases/" in (ROOT / ".gitignore").read_text()
    u = now["services"]["updater"]
    assert u["profiles"] == ["updater"] and u["restart"] == "unless-stopped"
    assert u["build"]["dockerfile"] == "Dockerfile.updater"
    assert "/var/run/docker.sock:/var/run/docker.sock" in u["volumes"]
    assert not any(":ro" in v and "docker.sock" in v for v in u["volumes"]), "the socket is read-write"
    config_mounts = [v for v in u["volumes"] if "config" in v]
    assert config_mounts == ["${PWD}/config/release_signing.pub:${PWD}/config/release_signing.pub:ro"], \
        "of config/, only the host's verification key, one file, read-only"
    assert not any("PRIVATE_KEY" in v or "restore-output" in v or v.split(":")[0].endswith(".key") for v in u["volumes"])
    assert u["command"][:3] == ["python", "-m", "core.components.updater"]
    assert (ROOT / "Dockerfile.updater").read_text().startswith("#") and "docker-compose" in (ROOT / "Dockerfile.updater").read_text()


def test_the_image_carries_release_and_vendored_and_git_ignores_the_build_products():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^COPY .*\bRELEASE\b.*\bVENDORED\.json\b.*BUILD\.json\*", dockerfile, re.M)
    ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "BUILD.json", "MANIFEST.sha256", "MANIFEST.sha256.sig",
                              "components.manifest.json", "config/retention_ruleset.yaml.previous", "releases/x", ".gates/last_green.json",
                              "config/release_signing.pub"],
                             capture_output=True, text=True).stdout.split()
    assert len(ignored) == 8, f"not every build product is ignored: {ignored}"
    assert not subprocess.run(["git", "-C", str(ROOT), "check-ignore", "RELEASE", "VENDORED.json"],
                              capture_output=True, text=True).stdout.strip(), "RELEASE and VENDORED.json belong in the tree"
    assert "config/release_signing.pub" not in subprocess.run(["git", "-C", str(ROOT), "ls-files", "config"],
                                                             capture_output=True, text=True).stdout, "no keys in the repository"


def test_components_schema_declares_the_ledger_and_the_journal_tables():
    sql = (ROOT / "core" / "db" / "components_schema.sql").read_text()
    for table in ("schema_migrations", "platform_updates", "platform_update_steps", "platform_backups",
                  "platform_component_acks", "platform_releases"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(", sql), f"no {table}"
    for col in ("name", "checksum", "applied_at", "applied_by", "note"):
        assert re.search(rf"^\s+{col}\s", sql.split("schema_migrations (")[1].split(");")[0], re.M)
    assert "WHERE finished_at = ''" in sql, "the open job row is the lock, enforced by the database too"


@pytest.mark.parametrize("rel", [
    "RELEASE", "core/components/build.py", "core/components/manifest.py", "core/components/members.py",
    "core/components/ledger.py", "core/components/journal.py", "core/components/updater.py",
    "core/components/vendored.py", "core/db/components_schema.sql", "scripts/components.py", "Dockerfile.updater",
])
def test_new_files_exist_and_carry_the_house_header(rel):
    path = ROOT / rel
    assert path.is_file(), rel
    text = path.read_text(encoding="utf-8")
    if rel != "RELEASE":
        assert "Ryan Gomez & Co. Inc." in "\n".join(text.splitlines()[:3]) or rel.endswith(".sql") or rel == "Dockerfile.updater"
        assert text.rstrip().endswith("Made by Ryan Gomez & Co. Inc."), f"{rel} does not end with the house comment"
# Made by Ryan Gomez & Co. Inc.



@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")
def test_docker_compose_renders_the_pinned_digest_onto_every_dockerfile_service(tmp_path):
    """`docker compose config` (client only, no daemon) with PHI_AI_IMAGE pinned:
    the five services resolve to the digest; unset, they resolve to phi-ai:dev."""
    compose_text = (ROOT / "docker-compose.yml").read_text()
    referenced = sorted(set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?::-[^}]*)?\}", compose_text)) - {"PWD", "PHI_AI_IMAGE"})

    def render(env_text: str) -> dict:
        # every other variable the file interpolates gets a placeholder, so an
        # unset key path cannot turn a volume spec into "::ro" and fail the render
        env = tmp_path / ".env"
        given = {line.split("=", 1)[0] for line in env_text.splitlines() if "=" in line}
        env.write_text(env_text + "".join(f"{name}=/placeholder/{name}\n" for name in referenced if name not in given))
        run = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "--env-file", str(env),
                              "--profile", "updater", "--profile", "verify", "config", "--format", "json"],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, run.stderr[-800:]
        return json.loads(run.stdout)["services"]
    pinned = render("PHI_AI_IMAGE=registry.example.org/phi-ai@sha256:" + "c" * 64 + "\n")
    for name in ("app", "web", "scheduler", "verify", "bulk-scheduler"):
        assert pinned[name]["image"] == "registry.example.org/phi-ai@sha256:" + "c" * 64, name
    assert pinned["viewer"]["image"] == "ohif/app:v3.13.4"
    assert pinned["updater"]["volumes"] and any(v.get("source", "").endswith("/releases") for v in pinned["updater"]["volumes"])
    unpinned = render("")
    for name in ("app", "web", "scheduler", "verify", "bulk-scheduler"):
        assert unpinned[name]["image"] == "phi-ai:dev", name


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")
def test_compose_renders_on_a_clone_that_has_no_env_file(tmp_path):
    """A FRESH CLONE renders docker-compose.yml. No .env, no --env-file.

    THIS IS THE CASE THE TEST ABOVE CANNOT SEE. It writes its own env file
    and passes --env-file, so it renders in a configuration no new
    contributor has: on a clean checkout `docker compose config` failed
    outright, and so did that test, because every service declared
    `env_file: .env` and .env is gitignored. README.md tells a new
    contributor to run the suite first and promises "the full suite runs
    without any cloud" - the suite was red until you had provisioned one.

    Two things had to be true and neither was:
      1. .env must be OPTIONAL - `required: false` - with a committed
         .env.defaults read before it for the values a render needs.
      2. Every ${VAR} INTERPOLATED into a volume spec needs a default in
         the compose file itself. env_file does not feed interpolation, so
         .env.defaults cannot fix this one: an unset key path rendered
         `::ro` and Compose rejected the file.

    Only the tracked files are copied, so this fails if a fix ever depends
    on something gitignored - which is the whole bug, restated.
    """
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=60)
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    clone = tmp_path / "clone"
    for rel in filter(None, tracked.stdout.split("\0")):
        src = ROOT / rel
        if not src.is_file():
            continue          # a deleted-but-staged path; nothing to copy
        dst = clone / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())

    assert (clone / "docker-compose.yml").is_file()
    assert (clone / ".env.defaults").is_file(), (
        ".env.defaults is not tracked - it is the fallback docker-compose.yml "
        "reads before .env, so it has to be in the clone"
    )
    assert not (clone / ".env").exists(), (
        ".env reached a clone of the tracked files. It is gitignored and holds "
        "a deployment's buckets and key paths; it must never be committed."
    )

    run = subprocess.run(
        ["docker", "compose", "--profile", "updater", "--profile", "verify",
         "config", "--format", "json"],
        cwd=clone, capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, (
        "docker compose config failed on a clone with no .env:\n"
        + run.stderr[-800:]
    )
    services = json.loads(run.stdout)["services"]
    for name in ("app", "web", "scheduler", "verify", "bulk-scheduler"):
        assert services[name]["image"] == "phi-ai:dev", name


# ---------------------------------------------------------------------------
# git's context variables override -C
# ---------------------------------------------------------------------------

def test_git_helpers_answer_about_the_directory_they_were_given(tmp_path, monkeypatch):
    """`-C` IS A CHDIR, NOT A SCOPE, and GIT_DIR beats it.

    FOUND WHILE PUBLISHING. scripts/pre_push_gates.sh runs this suite from
    a pre-push hook, and git exports GIT_DIR to its hooks. Every git call
    in this file therefore acted on the branch being pushed rather than on
    its own fixture: _git_repo() built a throwaway repository and committed
    "one" INTO THE REAL BRANCH, moving HEAD to a three-file tree in the
    middle of the push that was verifying it. The push was refused on
    unrelated grounds and the damage was found before anything reached a
    remote; on a green run it would have published.

    This pins both halves - the production reader and the test helper -
    with GIT_DIR pointed somewhere else entirely, which is the condition a
    hook creates.
    """
    fixture = _git_repo(tmp_path, {"RELEASE": "7.7.7\n"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q", "-b", "main")

    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(elsewhere))

    assert build.git(fixture, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert build.git(fixture, "log", "-1", "--format=%s") == "one", (
        "build.git() reported another repository's history; GIT_DIR won over -C"
    )
    assert _git(fixture, "log", "-1", "--format=%s") == "one"


def test_git_env_strips_every_variable_that_overrides_c(monkeypatch):
    for name in build.GIT_CONTEXT_VARS:
        monkeypatch.setenv(name, "/somewhere/else")
    env = build.git_env()
    assert not [n for n in build.GIT_CONTEXT_VARS if n in env]
    assert "PATH" in env, "the scrub must not empty the environment"


def test_the_release_manifest_enumerates_the_tree_it_was_pointed_at(tmp_path, monkeypatch):
    """THE MANIFEST IS WHAT A RELEASE IS VERIFIED AGAINST, so building it
    from the wrong repository is the worst form of this bug.

    scripts/components.py tracked_files() runs `git ls-files`. GIT_DIR
    overrides -C, and git exports GIT_DIR to its hooks - so a release
    built from inside any git-invoked context enumerated the INVOKING
    repository's files, and MANIFEST.sha256 was then signed over that
    list. Observed: the pre-push gate ran the build and the manifest came
    back naming another checkout's files.
    """
    import scripts.components as cli

    root = _git_repo(tmp_path, {"RELEASE": "9.9.9\n", "README.md": "hi\n", "core/x.py": "x = 1\n"})
    other_parent = tmp_path / "other"
    other_parent.mkdir()
    elsewhere = _git_repo(other_parent, {"DIFFERENT.md": "no\n"})

    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(elsewhere))

    assert cli.tracked_files(root) == ["README.md", "RELEASE", "core/x.py"], (
        "the manifest enumerated another repository's files"
    )
