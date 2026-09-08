# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The update journal and the five-step engine.

Every platform update, direct or guided, is one JOB that runs the same
five steps: Plan, Back up, Apply, Verify, Recover. The journal records
each step as it completes (platform_updates, platform_update_steps in
core/db/components_schema.sql); the open job row is the LOCK across every
service in the deployment - at most one job is open, and it names who
holds it and since when.

In-memory truth with SQL write-through, the PlatformState pattern: reads
never 500 on a database hiccup, and with a connection the job tables are
re-read before the lock is checked so two processes (the web app and the
updater service) see the same open job.

The rules the engine enforces, whichever mode:

  - nothing proceeds until the admin confirms the plan by typing
    "<component> <target>";
  - Back up may not be skipped: no verified backup, no apply;
  - a failed Verify moves the job to Recover; "recovered" is shown only
    after the recovery is verified;
  - a step the admin attests rather than the screen verifying is recorded
    as an attestation, visibly distinct;
  - every transition yields an audit-shaped event (action, resource_key,
    actor) in `events`, for the caller to record on the trail:
    system.update_step and system.component_acknowledged.

Direct executors: in direct mode the engine performs Back up, Apply,
Verify and Recover itself for the components that can be direct in this
process - operator config files (this module), model retirement (this
module) and image releases (the updater service, which holds the docker
socket and runs its own Journal against the same tables). A record-only
component has no job; a guided job records what the admin did.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import shutil
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from core.components.registry import (
    KEEP, MODES, STEPS, Context, load_members, now, registry,
)

log = logging.getLogger("phi-ai.components.journal")

STATUSES = ("planned", "confirmed", "running", "verify_failed", "recovering",
            "recovered", "done", "failed")
OUTCOMES = ("ok", "failed", "skipped")
STEP_STATUSES = ("pending", "done", "failed", "skipped")
CLOSED = ("done", "failed", "recovered")
ENGINE = "engine"
ACTION_STEP = "system.update_step"
ACTION_ACK = "system.component_acknowledged"


class JobOpen(RuntimeError):
    """A second job was asked for while one is open."""


def _stamp(clock: Callable[[], datetime]) -> str:
    return clock().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Direct executors: operator config files
# ---------------------------------------------------------------------------

def example_for(root: Path, name: str) -> Path:
    """config/<stem>.example.yaml for config/<stem>.yaml."""
    stem = name[:-len(".yaml")] if name.endswith(".yaml") else name
    return Path(root) / "config" / f"{stem}.example.yaml"


def missing_config_keys(example: Any, actual: Any, prefix: str = "") -> list[str]:
    """Dotted keys the example has and the operator's file lacks, mappings
    walked recursively. Lists are values, not structure."""
    if not isinstance(example, dict) or not isinstance(actual, dict):
        return []
    out = []
    for key, value in example.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in actual:
            out.append(path)
        else:
            out.extend(missing_config_keys(value, actual.get(key), path))
    return out


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def apply_operator_config(root: Path, name: str) -> dict:
    """Add the top-level keys the shipped example has and config/<name>
    lacks, with the example's defaults, keeping every existing line (the
    operator's comments included) and the previous file as <name>.previous.
    Nested keys missing under a key the file already has are reported, not
    applied: a text append cannot place them without rewriting the file."""
    root = Path(root)
    target = root / "config" / name
    example = example_for(root, name)
    if not example.is_file():
        raise FileNotFoundError(f"no shipped example {example.relative_to(root)} for {name}")
    example_data = _load_yaml(example)
    if not target.is_file():
        previous = None
        actual: dict = {}
        text = ""
    else:
        previous = target.with_name(target.name + ".previous")
        shutil.copyfile(target, previous)
        actual = _load_yaml(target)
        text = target.read_text(encoding="utf-8")
    if not isinstance(actual, dict):
        raise ValueError(f"{name} does not parse as a mapping")
    missing = missing_config_keys(example_data, actual)
    top_level = [k for k in missing if "." not in k]
    nested = [k for k in missing if "." in k]
    if top_level:
        block = "".join(
            "\n" + yaml.safe_dump({k: example_data[k]}, sort_keys=False, default_flow_style=False)
            for k in top_level)
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n# Added by the Components screen from {example.name}: keys the release expects.\n" + block.lstrip("\n")
        target.write_text(text, encoding="utf-8")
    return {"file": str(target.relative_to(root)),
            "previous": str(previous.relative_to(root)) if previous else None,
            "added": top_level, "nested_missing": nested,
            "sha256_before": _sha256(previous) if previous else None,
            "sha256_after": _sha256(target) if target.is_file() else None}


def restore_operator_config(root: Path, name: str, previous: Optional[str]) -> dict:
    """Put <name>.previous back; when there was no previous file, remove
    the one the apply created."""
    root = Path(root)
    target = root / "config" / name
    if previous:
        src = root / previous
        if not src.is_file():
            raise FileNotFoundError(f"{previous} is missing; nothing to restore from")
        shutil.copyfile(src, target)
        return {"file": str(target.relative_to(root)), "restored_from": previous,
                "sha256": _sha256(target)}
    if target.is_file():
        target.unlink()
    return {"file": str(target.relative_to(root)), "restored_from": None, "removed": True}


def verify_operator_config(root: Path, name: str) -> dict:
    root = Path(root)
    target = root / "config" / name
    example = example_for(root, name)
    if not target.is_file():
        raise FileNotFoundError(f"config/{name} is absent")
    missing = missing_config_keys(_load_yaml(example), _load_yaml(target))
    top_level = [k for k in missing if "." not in k]
    if top_level:
        raise ValueError(f"config/{name} still lacks {', '.join(top_level)}")
    return {"file": str(target.relative_to(root)), "missing": missing, "parses": True}


# ---------------------------------------------------------------------------
# Direct executors: the model catalogue
# ---------------------------------------------------------------------------

def _model_rows(platform_state: Any, model_id: str) -> list[dict]:
    return [m for m in platform_state.models if m.get("model_id") == model_id]


def retire_model(platform_state: Any, model_id: str, actor: str) -> dict:
    """Mark every registry row carrying this provider model id retired,
    remembering each row's previous status for recovery. Choosing the
    replacement stays the Control panel's register, enable, activate flow."""
    if platform_state is None:
        raise ValueError("no platform state in this process")
    with platform_state._lock:
        rows = _model_rows(platform_state, model_id)
        if not rows:
            raise ValueError(f"no registered model carries model_id {model_id!r}")
        previous = []
        for m in rows:
            previous.append({"id": m["id"], "name": m["name"], "previous_status": m["status"]})
            m["status"] = "retired"
            m["note"] = f"retired by {actor}: the provider retired {model_id}"
    for p in previous:
        platform_state._persist("UPDATE platform_models SET status = %s WHERE id = %s",
                                ("retired", p["id"]))
    return {"model_id": model_id, "rows": previous, "actor": actor}


def restore_model(platform_state: Any, rows: list[dict]) -> dict:
    """Put each row's previous status back."""
    if platform_state is None:
        raise ValueError("no platform state in this process")
    restored = []
    with platform_state._lock:
        for p in rows:
            for m in platform_state.models:
                if m["id"] == p["id"]:
                    m["status"] = p["previous_status"]
                    restored.append(p["id"])
    for p in rows:
        platform_state._persist("UPDATE platform_models SET status = %s WHERE id = %s",
                                (p["previous_status"], p["id"]))
    return {"restored": restored}


def verify_model_retired(platform_state: Any, model_id: str) -> dict:
    rows = _model_rows(platform_state, model_id)
    bad = [m["id"] for m in rows if m.get("status") != "retired"]
    if bad:
        raise ValueError(f"rows {bad} carrying {model_id!r} are not retired")
    return {"model_id": model_id, "retired_rows": [m["id"] for m in rows]}


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

class Journal:
    """The five-step engine with its journal; the API the screen and the
    updater code against."""

    def __init__(self, connection_factory=None, *, root: Optional[Path] = None,
                 platform_state: Any = None, updater: Any = None,
                 clock: Optional[Callable[[], datetime]] = None):
        self._connect = connection_factory
        self._lock = threading.RLock()
        self._clock = clock or now
        self.root = Path(root) if root else None
        self.platform_state = platform_state
        self.updater = updater
        self.jobs: dict[str, dict] = {}
        self.acks: list[dict] = []
        self.backup_rows: list[dict] = []
        self.release_rows: list[dict] = []
        self.events: list[tuple[str, str, str]] = []
        self._next_backup_id = 1
        self._next_ack_id = 1
        # True only when a database is there and was read: what an in-memory
        # journal records is lost with the process, and the screen says so.
        self.persistent = False
        if self._connect is not None:
            try:
                self._load_sql()
                self.persistent = True
            except Exception as exc:  # degraded, never fatal
                log.warning("journal DB load failed (in-memory journal in use): %s", exc)

    # ---- jobs ------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the job tables when there is a database: the lock is
        shared across services, so the freshest view decides it."""
        if self._connect is None:
            return
        try:
            self._load_sql(jobs_only=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("journal refresh failed (last-known jobs in use): %s", exc)

    def open_job(self) -> Optional[dict]:
        """The one open job, or None. The lock."""
        self.refresh()
        with self._lock:
            open_jobs = [j for j in self.jobs.values() if not j["finished_at"]]
            if not open_jobs:
                return None
            return copy.deepcopy(max(open_jobs, key=lambda j: j["started_at"]))

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self.jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def _component(self, key: str):
        comp = registry().get(key)
        if comp is None:
            comp = load_members().get(key)
        if comp is None:
            raise ValueError(f"unknown component {key!r}")
        return comp

    def _context(self) -> Context:
        return Context(root=self.root or Path.cwd(), platform_state=self.platform_state,
                       journal=self)

    def start_job(self, component_key: str, target: str, actor: str, mode: str = "guided") -> dict:
        comp = self._component(component_key)
        if comp.mode == "record":
            raise ValueError(f"{component_key} is record-only: it has no update to run")
        if mode not in MODES or mode == "record":
            raise ValueError(f"mode must be direct or guided, not {mode!r}")
        if mode == "direct" and comp.mode != "direct":
            raise ValueError(f"{component_key} cannot run direct; it is {comp.mode}")
        target = (target or "").strip()
        if not target:
            raise ValueError("a job needs a target: the version, digest, file or id it moves to")
        with self._lock:
            held = self.open_job()
            if held is not None:
                raise JobOpen(f"job {held['id']} on {held['component']} is open since "
                              f"{held['started_at']} by {held['started_by']}")
            steps = comp.steps(self._context())
            at = _stamp(self._clock)
            job = {
                "id": "upd_" + uuid.uuid4().hex[:12],
                "component": component_key, "target": target, "mode": mode,
                "status": "planned", "started_by": actor, "started_at": at, "finished_at": "",
                "plan": {"phrase": f"{component_key} {target}", "backup_unit": comp.backup_unit,
                         "recovery": comp.recovery,
                         "steps": [{"name": s.name, "direct_capable": s.direct_capable,
                                    "instruction": s.instruction, "verify": s.verify,
                                    "runbook": s.runbook} for s in steps]},
                "previous": {},
                "steps": [{"step": name, "status": "pending", "outcome": "", "actor": "", "at": "",
                           "attested": False, "mode": "", "evidence": None} for name in STEPS],
            }
            self.jobs[job["id"]] = job
            self._persist_job(job)
            self._event(ACTION_STEP, f"components/{component_key}/{job['id']}/Plan", actor)
            return copy.deepcopy(job)

    def confirm_plan(self, job_id: str, phrase: str, actor: str) -> dict:
        with self._lock:
            job = self._open(job_id)
            if job["status"] != "planned":
                raise ValueError(f"job {job_id} is {job['status']}; the plan was already confirmed")
            expected = f"{job['component']} {job['target']}"
            if (phrase or "").strip() != expected:
                raise ValueError("the confirmation must name the component and the target, "
                                 f"exactly: {expected!r}")
            self._set_step(job, "Plan", "done", "ok", actor, evidence={"phrase": expected})
            job["status"] = "confirmed"
            self._persist_job(job)
            self._event(ACTION_STEP, f"components/{job['component']}/{job_id}/Plan", actor)
            return copy.deepcopy(job)

    def next_step(self, job_id: str) -> Optional[str]:
        """The step waiting to run, or None when nothing is."""
        with self._lock:
            job = self.jobs[job_id]
            return self._next(job)

    def advance(self, job_id: str, step_name: str, outcome: str, actor: str, *,
                attested: bool = False, evidence: Any = None, mode: Optional[str] = None) -> dict:
        if step_name not in STEPS:
            raise ValueError(f"not one of the five steps: {step_name!r}")
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
        if step_name == "Plan":
            raise ValueError("Plan is confirmed with the typed phrase (confirm_plan), not advanced")
        if mode is not None and (mode not in MODES or mode == "record"):
            raise ValueError(f"a step runs direct or guided, not {mode!r}")
        with self._lock:
            job = self._open(job_id)
            if job["status"] in CLOSED:
                raise ValueError(f"job {job_id} is {job['status']}; finish it")
            if job["status"] == "planned":
                raise ValueError("confirm the plan first")
            if step_name == "Recover" and job["status"] not in ("verify_failed", "recovering"):
                raise ValueError("nothing to recover: Verify has not failed and no rollback was asked for")
            nxt = self._next(job)
            if nxt != step_name:
                raise ValueError(f"{step_name} is not the step waiting; "
                                 + (f"{nxt} is" if nxt else "nothing is: finish the job"))
            if step_name == "Back up" and outcome == "skipped":
                raise ValueError("Back up may not be skipped: no verified backup, no apply")
            if step_name == "Verify" and outcome == "skipped" and not attested:
                raise ValueError("Verify may be skipped only on the admin's attestation")
            step_mode = mode or job["mode"]
            if step_mode == "direct" and outcome == "ok":
                executor = self._executor(job, step_name)
                if executor is None:
                    raise ValueError(self._no_executor_reason(job, step_name))
                try:
                    evidence = executor(job)
                except Exception as exc:  # noqa: BLE001 - the outcome is the record
                    outcome = "failed"
                    evidence = {"error": f"{type(exc).__name__}: {exc}"[:400]}
            if step_mode == "direct" and outcome == "ok" and step_name == "Recover":
                # Recovered shows only after the second verification passes:
                # that the point of return is back, and the platform is healthy.
                verify = self._executor(job, "Verify recovery")
                if verify is not None:
                    try:
                        again = verify(job)
                        evidence = {"recover": evidence, "verify_again": again}
                    except Exception as exc:  # noqa: BLE001
                        outcome = "failed"
                        evidence = {"recover": evidence,
                                    "verify_again": {"error": f"{type(exc).__name__}: {exc}"[:400]}}
            self._set_step(job, step_name,
                           {"ok": "done", "failed": "failed", "skipped": "skipped"}[outcome],
                           outcome, actor, attested=attested, evidence=evidence, mode=step_mode)
            self._transition(job, step_name, outcome)
            self._persist_job(job)
            self._event(ACTION_STEP, f"components/{job['component']}/{job_id}/{step_name}", actor)
            return copy.deepcopy(job)

    def rollback(self, job_id: str, actor: str) -> dict:
        """Run the way back now. Direct: the engine recovers and verifies
        again. Guided: the job moves to Recover and waits on the admin."""
        with self._lock:
            job = self._open(job_id)
            if job["status"] in CLOSED or job["status"] == "planned":
                raise ValueError(f"job {job_id} is {job['status']}; nothing to roll back")
            applied = self._step(job, "Apply")["status"] == "done"
            recover = self._step(job, "Recover")
            if recover["status"] == "skipped" and recover["actor"] == ENGINE:
                recover.update(status="pending", outcome="", actor="", at="", attested=False, mode="", evidence=None)
            for name in ("Back up", "Apply", "Verify"):
                st = self._step(job, name)
                if st["status"] == "pending":
                    self._set_step(job, name, "skipped", "skipped", ENGINE,
                                   evidence={"note": "rolled back before this step ran"})
            if not applied:
                self._set_step(job, "Recover", "done", "ok", actor,
                               evidence={"note": "nothing was applied; nothing to recover"})
                job["status"] = "recovered"
                self._persist_job(job)
                self._event(ACTION_STEP, f"components/{job['component']}/{job_id}/Recover", actor)
                return copy.deepcopy(job)
            job["status"] = "recovering"
            self._persist_job(job)
            self._event(ACTION_STEP, f"components/{job['component']}/{job_id}/Recover", actor)
            if job["mode"] == "direct" and self._executor(job, "Recover") is not None:
                return self.advance(job_id, "Recover", "ok", actor)
            return copy.deepcopy(job)

    def finish(self, job_id: str, actor: str) -> dict:
        """Close the job and release the lock. What it closes as is what
        happened: done only when Verify passed; recovered after a verified
        recovery; failed otherwise. A job that still needs recovery cannot
        be closed."""
        with self._lock:
            job = self._open(job_id)
            status = job["status"]
            if status in ("verify_failed", "recovering"):
                raise ValueError(f"job {job_id} is {status}: recover (or roll back) before finishing")
            if status == "running":
                verify = self._step(job, "Verify")
                if verify["status"] == "done" or (verify["status"] == "skipped" and verify["attested"]):
                    job["status"] = "done"
                else:
                    job["status"] = "failed"
            elif status in ("planned", "confirmed"):
                job["status"] = "failed"
            for name in STEPS:
                if self._step(job, name)["status"] == "pending":
                    self._set_step(job, name, "skipped", "skipped", ENGINE,
                                   evidence={"note": f"not run: the job closed as {job['status']}"})
            job["finished_at"] = _stamp(self._clock)
            self._persist_job(job)
            self._event(ACTION_STEP, f"components/{job['component']}/{job_id}/{job['status']}", actor)
            return copy.deepcopy(job)

    def history(self, component_key: str, limit: int = 20) -> list[dict]:
        """Applies and recoveries for one component, newest first."""
        with self._lock:
            rows = [j for j in self.jobs.values() if j["component"] == component_key]
            rows.sort(key=lambda j: j["started_at"], reverse=True)
            return [copy.deepcopy(j) for j in rows[:limit]]

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = sorted(self.jobs.values(), key=lambda j: j["started_at"], reverse=True)
            return [copy.deepcopy(j) for j in rows[:limit]]

    def last_job(self) -> Optional[dict]:
        rows = self.recent(1)
        return rows[0] if rows else None

    # ---- acknowledgements ---------------------------------------------------

    def acknowledge(self, component_key: str, until_date: str, reason: str, actor: str) -> dict:
        """"Keep as is until <date> because <reason>", audited."""
        self._component(component_key)
        until = (until_date or "").strip()
        try:
            date.fromisoformat(until)
        except ValueError:
            raise ValueError("until_date must be a date, YYYY-MM-DD") from None
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("an acknowledgement needs a reason")
        with self._lock:
            row = {"id": self._next_ack_id, "component": component_key, "until_date": until,
                   "reason": reason[:400], "actor": actor, "at": _stamp(self._clock)}
            self._next_ack_id += 1
            self.acks.append(row)
            self._persist("INSERT INTO platform_component_acks (id, component, until_date, reason, actor, at) "
                          "VALUES (%s, %s, %s, %s, %s, %s)",
                          (row["id"], component_key, until, row["reason"], actor, row["at"]))
            self._event(ACTION_ACK, f"components/{component_key}", actor)
            return dict(row)

    def acknowledgements(self, component_key: str) -> list[dict]:
        with self._lock:
            rows = [a for a in self.acks if a["component"] == component_key]
            return [dict(a) for a in sorted(rows, key=lambda a: a["at"], reverse=True)]

    def acknowledged(self, component_key: str, today: Optional[date] = None) -> Optional[dict]:
        """The acknowledgement in force today, if any."""
        today = today or self._clock().date()
        for a in self.acknowledgements(component_key):
            if date.fromisoformat(a["until_date"]) >= today:
                return a
        return None

    # ---- backups -------------------------------------------------------------

    def backups(self) -> list[dict]:
        """Every backup recorded, newest first, each with the store's last
        rehearsal."""
        with self._lock:
            rehearsed = self._last_rehearsals()
            rows = [dict(r, rehearsed_at=rehearsed.get(r["store"], {}).get("at", ""),
                         rehearsed_by=rehearsed.get(r["store"], {}).get("actor", ""))
                    for r in self.backup_rows if r["kind"] == "backup"]
            return sorted(rows, key=lambda r: r["at"], reverse=True)

    def latest_backups(self) -> dict[str, dict]:
        """Per store: the newest backup row, with its rehearsal."""
        out: dict[str, dict] = {}
        for r in self.backups():
            out.setdefault(r["store"], r)
        return out

    def _last_rehearsals(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in sorted((r for r in self.backup_rows if r["kind"] == "rehearsal"), key=lambda r: r["at"]):
            out[r["store"]] = r
        return out

    def record_backup(self, store: str, location: str, checksum: str, actor: str, verified: bool) -> dict:
        """A backup taken. verified=True means the updater checksummed it;
        False records an attestation (an RDS snapshot the admin marked).
        KEEP backups per store: after a newer VERIFIED backup exists, the
        oldest beyond the count are dropped from the record."""
        store = (store or "").strip()
        if not store:
            raise ValueError("a backup names its store")
        with self._lock:
            row = {"id": self._next_backup_id, "kind": "backup", "store": store,
                   "location": location, "checksum": checksum, "verified": bool(verified),
                   "actor": actor, "at": _stamp(self._clock)}
            self._next_backup_id += 1
            self.backup_rows.append(row)
            self._persist("INSERT INTO platform_backups (id, kind, store, location, checksum, verified, actor, at) "
                          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                          (row["id"], "backup", store, location, checksum, bool(verified), actor, row["at"]))
            if verified:
                mine = sorted((r for r in self.backup_rows if r["kind"] == "backup" and r["store"] == store),
                              key=lambda r: r["at"])
                for old in mine[:-KEEP["backups"]] if len(mine) > KEEP["backups"] else []:
                    self.backup_rows.remove(old)
                    self._persist("DELETE FROM platform_backups WHERE id = %s", (old["id"],))
            return dict(row)

    def rehearsed(self, store: str, actor: str) -> dict:
        """A restore rehearsed for this store, dated."""
        store = (store or "").strip()
        if not store:
            raise ValueError("a rehearsal names its store")
        with self._lock:
            row = {"id": self._next_backup_id, "kind": "rehearsal", "store": store, "location": "",
                   "checksum": "", "verified": True, "actor": actor, "at": _stamp(self._clock)}
            self._next_backup_id += 1
            self.backup_rows.append(row)
            self._persist("INSERT INTO platform_backups (id, kind, store, location, checksum, verified, actor, at) "
                          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                          (row["id"], "rehearsal", store, "", "", True, actor, row["at"]))
            return dict(row)

    # ---- releases ------------------------------------------------------------

    def releases(self) -> list[dict]:
        """Every digest the journal knows, newest first, with its kept flag."""
        with self._lock:
            return [dict(r) for r in sorted(self.release_rows, key=lambda r: r["recorded_at"], reverse=True)]

    def kept_releases(self) -> list[dict]:
        return [r for r in self.releases() if r["kept"]]

    def record_release(self, digest: str, release: str, stamp: dict) -> dict:
        """A release image the deployment knows. KEEP images stay kept;
        older ones are marked not kept - the record of the release remains."""
        digest = (digest or "").strip()
        if not digest:
            raise ValueError("a release is recorded by its image digest")
        with self._lock:
            row = {"digest": digest, "release": release, "stamp": dict(stamp or {}),
                   "recorded_at": _stamp(self._clock), "kept": True}
            self.release_rows = [r for r in self.release_rows if r["digest"] != digest] + [row]
            ordered = sorted(self.release_rows, key=lambda r: r["recorded_at"], reverse=True)
            for i, r in enumerate(ordered):
                r["kept"] = i < KEEP["images"]
            for r in self.release_rows:
                self._persist("INSERT INTO platform_releases (digest, release, stamp, recorded_at, kept) "
                              "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (digest) DO UPDATE SET "
                              "release = EXCLUDED.release, stamp = EXCLUDED.stamp, "
                              "recorded_at = EXCLUDED.recorded_at, kept = EXCLUDED.kept",
                              (r["digest"], r["release"], json.dumps(r["stamp"]), r["recorded_at"], r["kept"]))
            return dict(row)

    # ---- internals -------------------------------------------------------------

    def _open(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError(f"no job {job_id}")
        if job["finished_at"]:
            raise ValueError(f"job {job_id} finished at {job['finished_at']}")
        return job

    @staticmethod
    def _step(job: dict, name: str) -> dict:
        return job["steps"][STEPS.index(name)]

    @staticmethod
    def _next(job: dict) -> Optional[str]:
        for st in job["steps"]:
            if st["status"] == "pending":
                return st["step"]
        return None

    def _set_step(self, job: dict, name: str, status: str, outcome: str, actor: str, *,
                  attested: bool = False, evidence: Any = None, mode: str = "") -> None:
        st = self._step(job, name)
        st.update(status=status, outcome=outcome, actor=actor, at=_stamp(self._clock),
                  attested=bool(attested), evidence=evidence, mode=mode or job["mode"])

    def _transition(self, job: dict, step_name: str, outcome: str) -> None:
        if step_name == "Back up":
            job["status"] = "running" if outcome == "ok" else "failed"
        elif step_name == "Apply":
            if outcome == "failed":
                self._set_step(job, "Verify", "skipped", "skipped", ENGINE,
                               evidence={"note": "apply failed; recovery is next"})
                job["status"] = "recovering"
            else:
                job["status"] = "running"
        elif step_name == "Verify":
            if outcome == "failed":
                job["status"] = "verify_failed"
            else:
                self._set_step(job, "Recover", "skipped", "skipped", ENGINE,
                               evidence={"note": "not needed: Verify passed"})
                job["status"] = "running"
        elif step_name == "Recover":
            job["status"] = "recovered" if outcome == "ok" else "failed"

    def _executor(self, job: dict, step_name: str) -> Optional[Callable[[dict], Any]]:
        """The function the engine runs for this step in direct mode, or
        None when this process cannot perform it."""
        comp, target = job["component"], job["target"]
        if comp == "operator_config" and self.root is not None:
            root = self.root
            if step_name == "Back up":
                def backup(j: dict) -> dict:
                    path = root / "config" / target
                    if not path.is_file():
                        j["previous"] = {"file": target, "previous": None}
                        return {"note": f"config/{target} is absent; nothing to copy"}
                    prev = path.with_name(path.name + ".previous")
                    shutil.copyfile(path, prev)
                    j["previous"] = {"file": target, "previous": str(prev.relative_to(root)),
                                     "sha256": _sha256(prev)}
                    return dict(j["previous"])
                return backup
            if step_name == "Apply":
                return lambda j: apply_operator_config(root, target)
            if step_name == "Verify":
                return lambda j: verify_operator_config(root, target)
            if step_name == "Recover":
                return lambda j: restore_operator_config(root, target, j["previous"].get("previous"))
            if step_name == "Verify recovery":
                def previous_is_back(j: dict) -> dict:
                    path = root / "config" / target
                    prev = j["previous"] or {}
                    if not prev.get("previous"):
                        if path.is_file():
                            raise ValueError(f"config/{target} exists though there was no file before the apply")
                        return {"file": f"config/{target}", "absent_as_before": True}
                    got = _sha256(path)
                    if got != prev.get("sha256"):
                        raise ValueError(f"config/{target} is not the previous file (sha256 {got[:12]})")
                    _load_yaml(path)
                    return {"file": f"config/{target}", "sha256": got, "previous_is_back": True}
                return previous_is_back
        if comp == "model_catalogue" and self.platform_state is not None:
            ps = self.platform_state
            if step_name == "Back up":
                def backup_rows(j: dict) -> dict:
                    rows = _model_rows(ps, target)
                    if not rows:
                        raise ValueError(f"no registered model carries model_id {target!r}")
                    j["previous"] = {"rows": [{"id": m["id"], "name": m["name"],
                                               "previous_status": m["status"]} for m in rows]}
                    return dict(j["previous"])
                return backup_rows
            if step_name == "Apply":
                return lambda j: retire_model(ps, target, j["started_by"])
            if step_name == "Verify":
                return lambda j: verify_model_retired(ps, target)
            if step_name == "Recover":
                return lambda j: restore_model(ps, j["previous"].get("rows", []))
            if step_name == "Verify recovery":
                def rows_are_back(j: dict) -> dict:
                    wanted = {p["id"]: p["previous_status"] for p in j["previous"].get("rows", [])}
                    got = {m["id"]: m["status"] for m in ps.models if m["id"] in wanted}
                    if got != wanted:
                        raise ValueError(f"rows not restored: {got} != {wanted}")
                    return {"restored": wanted}
                return rows_are_back
        if comp == "running_image" and self.updater is not None:
            up = self.updater
            if step_name == "Back up":
                def previous_digest(j: dict) -> dict:
                    prev = up.current_digest()
                    if not prev:
                        raise ValueError("no previous digest in the env file to return to; "
                                         "record the running digest (PHI_AI_IMAGE) first")
                    j["previous"] = {"digest": prev}
                    return {"digest": prev, "note": "the previous image digest is the backup unit; it stays kept"}
                return previous_digest
            if step_name == "Apply":
                return lambda j: {"pull": up.pull(target), "up": up.up(target)}
            if step_name in ("Verify", "Verify recovery"):
                return lambda j: up.healthcheck()
            if step_name == "Recover":
                return lambda j: up.rollback(j["previous"]["digest"])
        return None

    @staticmethod
    def _no_executor_reason(job: dict, step_name: str) -> str:
        comp = job["component"]
        if comp == "running_image":
            return (f"{step_name} of {comp} runs in the updater service, which holds the docker "
                    "socket; this process cannot perform it (run it guided, or start the updater)")
        if comp == "terminology":
            return ("terminology staging-and-swap is not yet implemented (slice 3); "
                    "run the step guided")
        if comp == "operator_config":
            return "this journal was built without a root; it cannot reach config/"
        if comp == "model_catalogue":
            return "this journal was built without the platform state; it cannot reach the registry"
        return f"{comp} has no direct executor; run the step guided"

    def _event(self, action: str, resource_key: str, actor: str) -> None:
        self.events.append((action, resource_key, actor))

    # ---- SQL mirror --------------------------------------------------------------

    def _persist(self, sql: str, params: tuple) -> None:
        if self._connect is None:
            return
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params)
                finally:
                    cur.close()
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # write-through is best-effort
            log.warning("journal persist failed (in-memory journal unaffected): %s", exc)

    def _persist_job(self, job: dict) -> None:
        self._persist(
            "INSERT INTO platform_updates (id, component, target, mode, status, started_by, started_at, "
            "finished_at, plan, previous) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, finished_at = EXCLUDED.finished_at, "
            "plan = EXCLUDED.plan, previous = EXCLUDED.previous",
            (job["id"], job["component"], job["target"], job["mode"], job["status"], job["started_by"],
             job["started_at"], job["finished_at"], json.dumps(job["plan"]), json.dumps(job["previous"])))
        for seq, st in enumerate(job["steps"]):
            self._persist(
                "INSERT INTO platform_update_steps (job_id, seq, step, status, outcome, actor, at, attested, "
                "mode, evidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (job_id, seq) DO UPDATE SET status = EXCLUDED.status, outcome = EXCLUDED.outcome, "
                "actor = EXCLUDED.actor, at = EXCLUDED.at, attested = EXCLUDED.attested, mode = EXCLUDED.mode, "
                "evidence = EXCLUDED.evidence",
                (job["id"], seq, st["step"], st["status"], st["outcome"], st["actor"], st["at"],
                 bool(st["attested"]), st["mode"], json.dumps(st["evidence"])))

    def _schema_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "db" / "components_schema.sql"

    def _load_sql(self, jobs_only: bool = False) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                if not jobs_only:
                    cur.execute(self._schema_path().read_text(encoding="utf-8"))
                cur.execute("SELECT id, component, target, mode, status, started_by, started_at, "
                            "finished_at, plan, previous FROM platform_updates ORDER BY started_at")
                jobs: dict[str, dict] = {}
                for r in cur.fetchall():
                    jobs[r[0]] = {"id": r[0], "component": r[1], "target": r[2], "mode": r[3],
                                  "status": r[4], "started_by": r[5], "started_at": r[6],
                                  "finished_at": r[7] or "", "plan": _json(r[8], {}),
                                  "previous": _json(r[9], {}),
                                  "steps": [{"step": name, "status": "pending", "outcome": "", "actor": "",
                                             "at": "", "attested": False, "mode": "", "evidence": None}
                                            for name in STEPS]}
                cur.execute("SELECT job_id, seq, step, status, outcome, actor, at, attested, mode, evidence "
                            "FROM platform_update_steps ORDER BY job_id, seq")
                for r in cur.fetchall():
                    job = jobs.get(r[0])
                    if job is None or not (0 <= int(r[1]) < len(STEPS)):
                        continue
                    job["steps"][int(r[1])] = {"step": r[2], "status": r[3], "outcome": r[4], "actor": r[5],
                                               "at": r[6] or "", "attested": bool(r[7]), "mode": r[8] or "",
                                               "evidence": _json(r[9], None)}
                with self._lock:
                    # The database's rows win for the ids it has; a job this
                    # process holds that never reached the database (a failed
                    # write-through) is kept rather than forgotten.
                    self.jobs.update(jobs)
                if jobs_only:
                    return
                cur.execute("SELECT id, component, until_date, reason, actor, at FROM platform_component_acks ORDER BY id")
                acks = [{"id": r[0], "component": r[1], "until_date": r[2], "reason": r[3], "actor": r[4], "at": r[5]}
                        for r in cur.fetchall()]
                cur.execute("SELECT id, kind, store, location, checksum, verified, actor, at FROM platform_backups ORDER BY id")
                backups = [{"id": r[0], "kind": r[1], "store": r[2], "location": r[3], "checksum": r[4],
                            "verified": bool(r[5]), "actor": r[6], "at": r[7]} for r in cur.fetchall()]
                cur.execute("SELECT digest, release, stamp, recorded_at, kept FROM platform_releases ORDER BY recorded_at")
                releases = [{"digest": r[0], "release": r[1], "stamp": _json(r[2], {}), "recorded_at": r[3],
                             "kept": bool(r[4])} for r in cur.fetchall()]
                with self._lock:
                    self.acks = acks
                    self.backup_rows = backups
                    self.release_rows = releases
                    self._next_ack_id = max((a["id"] for a in acks), default=0) + 1
                    self._next_backup_id = max((b["id"] for b in backups), default=0) + 1
            finally:
                cur.close()
            conn.commit()
        finally:
            conn.close()


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return default

# Made by Ryan Gomez & Co. Inc.
