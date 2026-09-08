# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
System: the Components screen - the platform half of the third System
screen (private-notes proposal of 2026-09-07, §10; every decision taken).

Every part of the platform that can go out of date is one row, declared
ONCE in core/components/registry.py's registry and enumerated here: what
is running, what it was built from, the latest known, where each fact
came from and when it was checked. The demonstration's screen shows
this layout with every row current and names each action; this screen
has the forms the demonstration only
names - same sections, same order, same words (Ryan's rule: same
experience, same words). It starts an update job, walks the five steps
of proposal §5 - plan, back up, apply, verify, recover - in the row's
mode, and records every transition.

House rules, applied:

- Role dictates visibility: reading is `system:admin`; applying is
  `system:update` on top of it. Both are enforced at the route, and the
  nav entry (core/web/nav.py) carries the read permission.
- Never assert unverified state: a Fact whose checked_at is None renders
  its reason in amber, never a value in green; the registry's state
  logic treats it as unread.
- Aggregates drill down: three cards, each opening to its rows; every
  row to its evidence, its procedure, its history and its
  acknowledgements.
- Patient context: the screen has no patient dimension and says so.
- No inline script (script-src 'self'): job progress renders on reload
  and on a <meta http-equiv="refresh"> while a job is open.
- A PHI host never phones home: "latest known" comes from the manifest
  the workstation CLI wrote; the manifest line goes amber past the
  advisory cadence.

The journal (core/components/journal.py, another builder's, with exactly
the methods MemoryJournal below has) is reached through
app.state.components_journal. When that module is absent, MemoryJournal
stands in so the screen works on a deployment that has not yet got the
engine - and the screen says that what it records is lost with the
process. Every journal transition is drained onto the audit trail under
the acting administrator's own name.
"""

from __future__ import annotations

import importlib
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.web.auth import Identity

# The registry MODULE. core/components/__init__.py re-exports the
# registry() accessor under the same name, so `from core.components import
# registry` would hand back the function; the dotted import cannot.
reg = importlib.import_module("core.components.registry")

log = logging.getLogger("phi-ai.web.components")

ROOT = Path(__file__).resolve().parents[2]

# The words the two screens share (the demonstration's screen uses the same):
# what the Action column offers per mode, and how a mode is named.
ACTION_WORD = {"direct": "Apply update", "guided": "Open checklist", "record": "Record only"}
MODE_WORD = {"direct": "direct", "guided": "guided", "record": "record only"}
# Green is a reading the row's source supports; amber is a reading nobody
# has (or one being changed right now); red is a reading that disagrees
# with its source.
STATE_CLASS = {"current": "v-good", "unknown": "v-amber", "updating": "v-amber",
               "behind": "v-warn", "drifted": "v-warn"}
STATUS_WORD = {
    "planned": "planned, waiting for the plan to be confirmed",
    "confirmed": "confirmed, waiting on Back up",
    "running": "running",
    "verify_failed": "verification failed, recovering",
    "recovering": "recovering",
    "recovered": "recovered",
    "done": "done",
    "failed": "failed",
}
# How a step reads on the screen: (class, mark) per status word.
STEP_MARK = {
    "done": ("v-good", "✓ done"),
    "not needed": ("v-good", "✓ not needed"),
    "failed": ("v-warn", "✗ failed"),
    "waiting on you": ("v-amber", "● waiting on you"),
    "the updater's turn": ("v-amber", "● the updater's turn"),
    "pending": ("v-mute", "○ pending"),
}

# Ryan's numbers, read from the one place they live (registry.CADENCE_DAYS,
# proposal §14 decision 4): the manifest line goes amber past the advisory
# cadence and a restore rehearsal falls due on its own.
MANIFEST_MAX_AGE = timedelta(days=reg.CADENCE_DAYS["advisories"])
REHEARSAL_DUE = timedelta(days=reg.CADENCE_DAYS["rehearsal"])
OPEN_JOB_REFRESH_SECONDS = 20
RECENT_ACTIONS = 12
OUTCOMES = ("ok", "failed")
CLOSED = frozenset({"done", "recovered", "failed"})
# The actions this screen writes; "recent component actions" is the audit
# trail narrowed to them.
AUDIT_PREFIXES = ("system.components", "system.component_", "system.update", "system.restore_")


class JobOpen(Exception):
    """A job is open: the open job row is the lock across every service."""


# ---------------------------------------------------------------------------
# Time, in the demo's words
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_when(value: Any) -> Optional[datetime]:
    """A datetime from a datetime, an ISO string or nothing; naive values
    are read as UTC, unparseable ones as unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ago(when: Any, now: Optional[datetime] = None) -> str:
    """"just now", "3 hours ago", "2 days ago" - the demo's cx_ago(), on the
    platform's own clock - or "never" when there is no time to read."""
    dt = _parse_when(when)
    if dt is None:
        return "never"
    seconds = max(0, int(((now or _now()) - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        n = seconds // 60
        return f"{n} minute{'' if n == 1 else 's'} ago"
    if seconds < 86400:
        n = seconds // 3600
        return f"{n} hour{'' if n == 1 else 's'} ago"
    n = seconds // 86400
    return f"{n} day{'' if n == 1 else 's'} ago"


def store_slug(name: str) -> str:
    """A store's URL segment: "Records (object store)" -> records-object-store."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "store"


# ---------------------------------------------------------------------------
# Jobs: what the screen reads off a job dict, whichever journal wrote it
# ---------------------------------------------------------------------------

def job_is_open(job: Optional[dict]) -> bool:
    return bool(job) and job.get("status") not in CLOSED


def step_records(job: Optional[dict]) -> dict[str, dict]:
    """The latest record per step name."""
    out: dict[str, dict] = {}
    for rec in (job or {}).get("steps") or ():
        if isinstance(rec, dict) and rec.get("step"):
            out[str(rec["step"])] = rec
    return out


def current_step(job: dict) -> tuple[int, str]:
    """The 1-based index and name of the step waiting on someone."""
    if job.get("status") in ("verify_failed", "recovering"):
        return len(reg.STEPS), "Recover"
    recs = step_records(job)
    for i, name in enumerate(reg.STEPS, 1):
        rec = recs.get(name)
        if rec is None or rec.get("status") != "done":
            return i, name
    return len(reg.STEPS), "Recover"


# ---------------------------------------------------------------------------
# The journal's stand-in
# ---------------------------------------------------------------------------

# The stores of proposal §6, with nothing recorded against them until the
# engine (core/components/journal.py) records something. Retention counts
# come from the registry, not from here.
_STORES = (
    ("Records (object store)", "S3 store bucket, versioning and KMS envelope",
     "bucket versioning and lifecycle"),
    ("Index (Postgres)", "RDS automated backup; rebuildable from S3 by core.db.reconcile",
     "the RDS backup retention variable"),
    ("Operational state (platform_* tables)", "store bucket, system/backups/ prefix, same KMS key",
     f"{reg.KEEP['backups']} per store"),
    ("config/", "beside the operational-state dump", f"{reg.KEEP['backups']} per store"),
    (".env", "never copied; presence and checksum recorded", "n/a: secrets stay where they are"),
    ("Vocabulary schemas", "the previous schema, kept on swap", "one previous schema"),
)


class MemoryJournal:
    """The journal's stand-in: the contract's methods, in memory.

    Used when core.components.journal is not importable (or cannot open its
    database), so the screen works before the engine lands. What it records
    is lost with the process, and the screen says so (`persistent`)."""

    persistent = False

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        self._acks: dict[str, list[dict]] = {}
        self._backups: list[dict] = [
            {"store": name, "where": where, "retention": keep, "last_at": None,
             "checksum": None, "rehearsed_at": None, "rehearsed_by": None}
            for name, where, keep in _STORES]
        self._releases: list[dict] = []
        # (action, resource_key, actor) per transition; the route drains
        # these onto the audit trail.
        self.events: list[tuple[str, str, str]] = []

    # -- jobs -----------------------------------------------------------

    def open_job(self) -> Optional[dict]:
        for job_id in reversed(self._order):
            job = self._jobs[job_id]
            if job["status"] not in CLOSED:
                return job
        return None

    def _job(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"no job {job_id!r}")
        return job

    @staticmethod
    def _step(name: str, status: str, outcome: str, actor: str,
              attested: bool = False, evidence: Optional[str] = None) -> dict:
        return {"step": name, "status": status, "outcome": outcome, "actor": actor,
                "at": _iso(_now()), "attested": bool(attested), "evidence": evidence}

    def _close(self, job: dict, status: str) -> None:
        job["status"] = status
        job["finished_at"] = _iso(_now())

    def start_job(self, component_key: str, target: str, actor: str, mode: str) -> dict:
        if mode not in ("direct", "guided"):
            raise ValueError(f"not an update mode: {mode!r}")
        held = self.open_job()
        if held is not None:
            raise JobOpen(f"a job is open on {held['component']}, held by "
                          f"{held['started_by']} since {held['started_at']}")
        job = {"id": "job_" + secrets.token_hex(6), "component": component_key,
               "target": target, "mode": mode, "status": "planned",
               "started_by": actor, "started_at": _iso(_now()), "finished_at": None,
               "steps": []}
        self._jobs[job["id"]] = job
        self._order.append(job["id"])
        self.events.append(("system.update_started",
                            f"components/{component_key} target={target} mode={mode}", actor))
        return job

    def confirm_plan(self, job_id: str, phrase: str, actor: str) -> dict:
        job = self._job(job_id)
        if job["status"] != "planned":
            raise ValueError("the plan was already confirmed")
        if (phrase or "").strip() != f"{job['component']} {job['target']}":
            raise ValueError("the phrase must name the component and the target, exactly")
        job["status"] = "confirmed"
        job["steps"].append(self._step("Plan", "done", "ok", actor))
        self.events.append(("system.update_confirmed",
                            f"components/{job['component']} job={job_id}", actor))
        return job

    def advance(self, job_id: str, step_name: str, outcome: str, actor: str, *,
                attested: bool = False, evidence: Optional[str] = None,
                mode: Optional[str] = None) -> dict:
        job = self._job(job_id)
        if job["status"] in CLOSED:
            raise ValueError("the job is closed")
        if job["status"] == "planned":
            raise ValueError("confirm the plan first")
        if step_name not in reg.STEPS:
            raise ValueError(f"not one of the five steps: {step_name!r}")
        if outcome not in OUTCOMES:
            raise ValueError(f"an outcome is ok or failed, not {outcome!r}")
        _, waiting = current_step(job)
        if step_name != waiting:
            raise ValueError(f"the step waiting is {waiting}, not {step_name}")
        # A downgrade to guided is always allowed; an upgrade never is.
        if mode == "guided":
            job["mode"] = "guided"
        elif mode not in (None, "", job["mode"]):
            raise ValueError("the mode can only be downgraded to guided")
        job["steps"].append(self._step(step_name, "done" if outcome == "ok" else "failed",
                                       outcome, actor, attested, evidence))
        self.events.append(("system.update_step",
                            f"components/{job['component']} {step_name}={outcome} "
                            f"mode={job['mode']}" + (" attested" if attested else ""), actor))
        if outcome == "failed":
            if step_name in ("Back up", "Recover"):
                # Fails closed: no verified backup, no apply; a recovery that
                # failed is a job that failed, for a person to finish by hand.
                self._close(job, "failed")
            elif step_name == "Verify":
                job["status"] = "verify_failed"
            else:
                job["status"] = "recovering"
        elif step_name == "Verify":
            job["steps"].append(self._step("Recover", "done", "not needed", actor))
            self._close(job, "done")
        elif step_name == "Recover":
            self._close(job, "recovered")
        else:
            job["status"] = "running"
        return job

    def rollback(self, job_id: str, actor: str) -> dict:
        job = self._job(job_id)
        if job["status"] in CLOSED:
            raise ValueError("the job is closed")
        applied = any(s["step"] == "Apply" and s["status"] == "done" for s in job["steps"])
        self.events.append(("system.update_rolled_back",
                            f"components/{job['component']} job={job_id}", actor))
        if applied:
            job["status"] = "recovering"
        else:
            # Nothing was touched: the way back is to stop.
            job["steps"].append(self._step("Recover", "done", "not needed", actor))
            self._close(job, "recovered")
        return job

    def finish(self, job_id: str, actor: str) -> dict:
        job = self._job(job_id)
        if job["status"] in CLOSED:
            raise ValueError("the job is closed")
        if not any(s["step"] == "Verify" and s["status"] == "done" for s in job["steps"]):
            raise ValueError("Verify has not passed; record the steps, or roll back")
        self._close(job, "done")
        self.events.append(("system.update_finished",
                            f"components/{job['component']} job={job_id}", actor))
        return job

    def _entry(self, job: dict) -> dict:
        done = sum(1 for s in job["steps"] if s["status"] == "done")
        status = job["status"]
        if status == "done":
            outcome = f"{done} of {len(reg.STEPS)} steps done, no recovery needed"
        elif status == "recovered":
            outcome = "rolled back and recovered"
        elif status == "failed":
            last = job["steps"][-1]["step"] if job["steps"] else "Plan"
            outcome = f"failed at {last}"
        else:
            outcome = f"open: {STATUS_WORD.get(status, status)}"
        return {"id": job["id"], "component": job["component"],
                "what": f"update to {job['target']}", "target": job["target"],
                "mode": job["mode"], "status": status, "outcome": outcome,
                "at": job["finished_at"] or job["started_at"],
                "started_by": job["started_by"],
                "steps": [(s["step"], s["status"] if s["outcome"] != "not needed" else "not needed",
                           s["actor"]) for s in job["steps"]]}

    def history(self, component_key: str, limit: int = 20) -> list[dict]:
        out: list[dict] = []
        for job_id in reversed(self._order):
            job = self._jobs[job_id]
            if job["component"] != component_key:
                continue
            out.append(self._entry(job))
            if len(out) >= limit:
                break
        return out

    # -- acknowledgements, backups, releases ----------------------------

    def acknowledge(self, component_key: str, until_date: Any, reason: str, actor: str) -> dict:
        until = until_date.isoformat() if hasattr(until_date, "isoformat") else str(until_date)
        ack = {"component": component_key, "until": until, "reason": reason,
               "actor": actor, "at": _iso(_now())}
        self._acks.setdefault(component_key, []).append(ack)
        self.events.append(("system.component_acknowledged",
                            f"components/{component_key} until={until}", actor))
        return ack

    def acknowledgements(self, component_key: str) -> list[dict]:
        return list(reversed(self._acks.get(component_key, [])))

    def backups(self) -> list[dict]:
        return [dict(b) for b in self._backups]

    def rehearsed(self, store: str, actor: str) -> dict:
        for b in self._backups:
            if b["store"] == store:
                b["rehearsed_at"] = _iso(_now())
                b["rehearsed_by"] = actor
                self.events.append(("system.restore_rehearsed", f"backups/{store_slug(store)}", actor))
                return dict(b)
        raise KeyError(f"no store {store!r}")

    def releases(self) -> list[dict]:
        return [dict(r) for r in self._releases]


def default_journal(connection_factory=None, *, root: Optional[Path] = None, platform_state=None):
    """The real journal when its module is importable, else the stand-in.
    The real one gets the install root and the platform state: its direct
    executors (operator config, model retirement) act on them."""
    try:
        from core.components.journal import Journal
    except ImportError:
        return MemoryJournal()
    try:
        return Journal(connection_factory=connection_factory, root=root or ROOT,
                       platform_state=platform_state)
    except Exception as exc:  # noqa: BLE001 - degraded, never fatal
        log.warning("components: journal unavailable (%s); an in-memory stand-in is in use", exc)
        return MemoryJournal()


# ---------------------------------------------------------------------------
# Readers: the registry and its context, tolerant of what is not there yet
# ---------------------------------------------------------------------------

def _registry() -> dict[str, reg.Component]:
    """The real declarations when members.py is importable; otherwise
    whatever the registry holds (a test's fakes, or nothing - and nothing
    renders as "no components declared", never as a 500)."""
    try:
        return reg.load_members()
    except ImportError as exc:
        log.debug("components: members not importable (%s)", exc)
        return reg.registry()


def _manifest(root: Path) -> Optional[dict]:
    try:
        from core.components import manifest
    except ImportError:
        return None
    try:
        return manifest.load(root)
    except Exception as exc:  # noqa: BLE001 - a bad manifest is an amber line, not a 500
        log.warning("components: manifest unreadable: %s", exc)
        return None


def _build(root: Path) -> Optional[dict]:
    try:
        from core.components import build
    except ImportError:
        return None
    try:
        return build.read_stamp(root)
    except Exception as exc:  # noqa: BLE001
        log.warning("components: build stamp unreadable: %s", exc)
        return None


def _context(app, open_job: Optional[dict]) -> reg.Context:
    platform_state = getattr(app.state, "platform_state", None)
    return reg.Context(root=ROOT,
                       connect=getattr(platform_state, "_connect", None),
                       platform_state=platform_state,
                       manifest=_manifest(ROOT), build=_build(ROOT),
                       open_job_key=open_job.get("component") if open_job else None,
                       journal=_journal(app))


def _updater_reachable(app) -> bool:
    """Direct mode needs the updater service (proposal §9). Probed, never
    assumed: absent or unreachable means guided."""
    updater = getattr(app.state, "components_updater", None)
    if updater is None:
        return False
    probe = getattr(updater, "reachable", None)
    try:
        return bool(probe()) if callable(probe) else bool(updater)
    except Exception as exc:  # noqa: BLE001
        log.warning("components: updater probe failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Views: dicts the template renders, built from the registry and the journal
# ---------------------------------------------------------------------------

def _fact(f: reg.Fact, now: datetime) -> dict:
    return {"value": f.value, "source": f.source, "href": f.href, "known": f.known,
            "checked": ago(f.checked_at, now)}


def _cadence(component: reg.Component) -> str:
    if component.cadence_days is not None:
        return f"every {component.cadence_days} days"
    return "none — record only" if component.mode == "record" else "none — on demand"


def _row(component: reg.Component, reading: reg.Reading, now: datetime) -> dict:
    sources: list[str] = []
    for f in (reading.running, reading.built, reading.latest):
        if f.source and f.source not in sources:
            sources.append(f.source)
    href = next((f.href for f in (reading.latest, reading.built, reading.running) if f.href), None)
    return {"key": component.key, "group": component.group, "name": component.name,
            "kind": component.kind, "mode": component.mode,
            "mode_word": MODE_WORD[component.mode], "action_word": ACTION_WORD[component.mode],
            "backup_unit": component.backup_unit, "recovery": component.recovery,
            "cadence": _cadence(component),
            "running": _fact(reading.running, now), "built": _fact(reading.built, now),
            "latest": _fact(reading.latest, now),
            "source": {"label": " · ".join(sources) or "unknown", "href": href},
            "latest_source": {"label": reading.latest.source or "unknown", "href": reading.latest.href},
            "checked": ago(reading.checked_at, now), "checked_known": reading.checked_at is not None,
            "state": reading.state, "state_class": STATE_CLASS.get(reading.state, "v-amber"),
            "note": reading.note}


def _manifest_line(manifest: Optional[dict], now: datetime) -> dict:
    """"Manifest produced <when> by <who> on <host>", amber past its age."""
    if not manifest:
        # The same words as with a manifest, and the truth: never.
        return {"produced": None, "by": None, "host": None, "amber": True,
                "text": "Manifest produced never: none on this host. Run scripts/components.py "
                        "check on the workstation and ship the result."}
    produced = _parse_when(manifest.get("produced_at"))
    by = str(manifest.get("produced_by") or manifest.get("by") or "unknown")
    host = str(manifest.get("host") or manifest.get("hostname") or "unknown")
    amber = produced is None or (now - produced) > MANIFEST_MAX_AGE
    return {"produced": ago(produced, now), "by": by, "host": host, "amber": amber, "text": None}


def _job_view(job: dict, registry: dict[str, reg.Component], now: datetime) -> dict:
    component = registry.get(str(job.get("component", "")))
    n, step = current_step(job)
    status = str(job.get("status", ""))
    return {"id": job.get("id"), "component": job.get("component"),
            "name": component.name if component else job.get("component"),
            "target": job.get("target"), "mode": job.get("mode"),
            "mode_word": MODE_WORD.get(str(job.get("mode")), str(job.get("mode"))),
            "status": status, "status_word": STATUS_WORD.get(status, status),
            "started_by": job.get("started_by"), "since": ago(job.get("started_at"), now),
            "n": n, "step": step, "open": job_is_open(job),
            "phrase": f"{job.get('component')} {job.get('target')}"}


def _step_rows(steps: tuple[reg.Step, ...], job: Optional[dict], now: datetime) -> list[dict]:
    """The five steps as the screen shows them, each with its mark."""
    recs = step_records(job) if job else {}
    current = current_step(job)[1] if job_is_open(job) else None
    direct = bool(job) and job.get("mode") == "direct"
    rows = []
    for i, step in enumerate(steps, 1):
        rec = recs.get(step.name)
        word: Optional[str]
        if rec is not None:
            word = "not needed" if rec.get("outcome") == "not needed" else (
                "done" if rec.get("status") == "done" else "failed")
        elif current == step.name:
            word = "the updater's turn" if direct and step.direct_capable else "waiting on you"
        elif job:
            word = "pending"
        else:
            word = None
        mark_class, mark = STEP_MARK.get(word, ("", "")) if word else ("", "")
        record_view = None
        if rec is not None:
            record_view = {"actor": rec.get("actor"), "when": ago(rec.get("at"), now),
                           "attested": bool(rec.get("attested")), "evidence": rec.get("evidence")}
        rows.append({"n": i, "step": step, "word": word, "mark_class": mark_class, "mark": mark,
                     "current": current == step.name, "record": record_view,
                     "who": "the updater" if direct and step.direct_capable else "you"})
    return rows


def _history_view(entry: dict, now: datetime) -> dict:
    steps = []
    for s in entry.get("steps") or ():
        if isinstance(s, dict):
            steps.append((s.get("step"), s.get("status"), s.get("actor")))
        else:
            parts = tuple(s) + (None, None, None)
            steps.append((parts[0], parts[1], parts[2]))
    outcome = str(entry.get("outcome") or entry.get("status") or "")
    return {"component": entry.get("component"),
            "what": entry.get("what") or f"update to {entry.get('target', '')}".strip(),
            "when": ago(entry.get("at") or entry.get("finished_at") or entry.get("started_at"), now),
            "mode": entry.get("mode"), "mode_word": MODE_WORD.get(str(entry.get("mode")), str(entry.get("mode"))),
            "outcome": outcome,
            "outcome_class": "v-good" if entry.get("status") in ("done", "recovered") else (
                "v-warn" if entry.get("status") == "failed" else "v-amber"),
            "steps": steps, "at_key": str(entry.get("at") or entry.get("finished_at") or entry.get("started_at") or "")}


def _backup_view(b: dict, now: datetime) -> dict:
    name = str(b.get("store") or b.get("name") or "")
    rehearsed = _parse_when(b.get("rehearsed_at") or b.get("rehearsed"))
    last = _parse_when(b.get("last_at") or b.get("last_backup_at") or b.get("last"))
    return {"store": name, "slug": store_slug(name),
            "last": ago(last, now), "last_known": last is not None,
            "where": str(b.get("where") or "unknown"),
            "checksum": str(b.get("checksum") or "not recorded"),
            "checksum_known": bool(b.get("checksum")),
            "rehearsed": ago(rehearsed, now),
            "rehearsed_due": rehearsed is None or (now - rehearsed) > REHEARSAL_DUE,
            "rehearsed_by": b.get("rehearsed_by"),
            "retention": str(b.get("retention") or "unknown")}


def _release_view(r: dict) -> dict:
    return {"release": str(r.get("release") or r.get("tag") or "unknown"),
            "date": str(r.get("date") or r.get("built_at") or "unknown"),
            "commit": str(r.get("commit") or r.get("sha") or "unknown"),
            "digest": r.get("digest"), "kept": r.get("kept")}


def _recent(reader, now: datetime) -> list[dict]:
    """The audit trail narrowed to this screen's actions, newest first."""
    try:
        events = reader.read_audit_events(limit=200)
    except Exception as exc:  # noqa: BLE001 - the trail's own page handles its failures
        log.warning("components: audit events unavailable: %s", exc)
        return []
    mine = [e for e in events if str(e.get("action", "")).startswith(AUDIT_PREFIXES)]
    mine.sort(key=lambda e: str(e.get("timestamp") or ""), reverse=True)
    return [{"when": ago(e.get("timestamp"), now), "actor": e.get("actor"),
             "action": e.get("action"), "object": e.get("resource_key")}
            for e in mine[:RECENT_ACTIONS]]


def _last_entry(journal, registry: dict[str, reg.Component], now: datetime) -> Optional[dict]:
    """The most recent apply or recovery across the registry, for the
    Update journal section. The contract's history() is per component, so
    this is one bounded read per row; an admin screen can afford it."""
    entries: list[dict] = []
    for key in registry:
        try:
            entries.extend(journal.history(key, limit=1) or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("components: history of %s unavailable: %s", key, exc)
    if not entries:
        return None
    views = [_history_view(e, now) for e in entries]
    views.sort(key=lambda v: v["at_key"], reverse=True)
    view = views[0]
    component = registry.get(str(view["component"]))
    view["name"] = component.name if component else view["component"]
    return view


def maintenance_line(app) -> Optional[dict]:
    """The banner base.html shows on every page while a job is open; None
    otherwise. Never raises: a page must not fail because the journal is
    unreachable."""
    try:
        job = _journal(app).open_job()
        if not job_is_open(job):
            return None
        n, step = current_step(job)
        key = str(job.get("component", ""))
        try:
            component = _registry().get(key)
        except Exception:  # noqa: BLE001
            component = None
        return {"component": component.name if component else key, "step": n,
                "of": len(reg.STEPS), "step_name": step, "href": f"/system/components/{key}"}
    except Exception as exc:  # noqa: BLE001
        log.warning("components: maintenance banner unavailable: %s", exc)
        return None


def _journal(app):
    journal = getattr(app.state, "components_journal", None)
    if journal is None:
        platform_state = getattr(app.state, "platform_state", None)
        journal = default_journal(getattr(platform_state, "_connect", None),
                                  root=ROOT, platform_state=platform_state)
        app.state.components_journal = journal
    return journal


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

def register(app, page, require, current_identity, record, reader) -> None:
    _journal(app)

    def _drain(journal, identity: Identity, fallback: Optional[tuple[str, str]] = None) -> None:
        """Write the journal's pending events to the audit trail under the
        acting identity, then the route's own event unless the journal
        already named that action - a hole in the trail is worse than a
        repeat, and the repeat is avoided by looking."""
        seen: set[str] = set()
        events = getattr(journal, "events", None)
        if isinstance(events, list):
            while events:
                action, resource_key, actor = events.pop(0)
                if actor != identity.username:
                    log.warning("components: journal event by %s drained under %s",
                                actor, identity.username)
                seen.add(action)
                record(identity, action, resource_key, "operations")
        if fallback and fallback[0] not in seen:
            record(identity, fallback[0], fallback[1], "operations")

    def _component(registry: dict[str, reg.Component], key: str) -> reg.Component:
        component = registry.get(key)
        if component is None or component.demo_only:
            raise HTTPException(status_code=404,
                                detail="No such component: the registry does not list it. "
                                       "Open the Components screen for every row it has.")
        return component

    def _open_job_of(journal, job_id: str) -> dict:
        """Only the open job can be acted on, and only by its id."""
        job = journal.open_job()
        if not job_is_open(job) or str(job.get("id")) != job_id:
            raise HTTPException(status_code=404,
                                detail="No such open job. The screen shows the job that is "
                                       "open, if any; a closed job is history.")
        return job

    def _refused(request: Request, identity: Identity, status: int, title: str, detail: str):
        journal = _journal(app)
        return page(request, "components.html", identity, status_code=status,
                    active="components", refusal={"status": status, "title": title, "detail": detail},
                    open_job=journal.open_job() if job_is_open(journal.open_job()) else None,
                    refresh=OPEN_JOB_REFRESH_SECONDS)

    def _detail(request: Request, identity: Identity, key: str, *, error: Optional[str] = None,
                status_code: int = 200):
        journal = _journal(app)
        registry = _registry()
        component = _component(registry, key)
        now = _now()
        open_job = journal.open_job()
        open_job = open_job if job_is_open(open_job) else None
        ctx = _context(app, open_job)
        reading = component.read(ctx)
        procedure_error = None
        try:
            steps = component.steps(ctx)
        except ValueError as exc:
            # A bad declaration is shown on the row it belongs to, not
            # turned into a screen that will not open.
            steps, procedure_error = (), str(exc)
        job = open_job if open_job and open_job.get("component") == key else None
        row = _row(component, reading, now)
        try:
            history = [_history_view(h, now) for h in (journal.history(key) or [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("components: history unavailable: %s", exc)
            history = []
        try:
            acks = [{"until": a.get("until"), "reason": a.get("reason"), "actor": a.get("actor"),
                     "when": ago(a.get("at"), now)} for a in (journal.acknowledgements(key) or [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("components: acknowledgements unavailable: %s", exc)
            acks = []
        open_component = registry.get(str(open_job.get("component"))) if open_job else None
        return page(request, "components.html", identity, status_code=status_code,
                    active="components", row=row, group_title=reg.GROUPS[component.group],
                    evidence=reading.evidence,
                    step_rows=_step_rows(steps, job, now), steps_total=len(reg.STEPS),
                    procedure_error=procedure_error,
                    job=_job_view(job, registry, now) if job else None,
                    open_job=open_job,
                    open_job_name=open_component.name if open_component else (
                        open_job.get("component") if open_job else None),
                    history=history, acks=acks, error=error,
                    can_update=identity.can("system:update"),
                    updater_reachable=_updater_reachable(app),
                    today=date.today().isoformat(),
                    journal_persistent=getattr(journal, "persistent", True),
                    refresh=OPEN_JOB_REFRESH_SECONDS)

    # ---- the screen ---------------------------------------------------

    @app.get("/system/components", response_class=HTMLResponse)
    def components_screen(request: Request,
                          identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        journal = _journal(app)
        registry = _registry()
        now = _now()
        open_job = journal.open_job()
        open_job = open_job if job_is_open(open_job) else None
        ctx = _context(app, open_job)
        readings = reg.read_all(ctx, registry)
        rows = [_row(c, r, now) for c, r in readings]
        cards = reg.summary(readings)
        # The visit is on the trail before the trail is read, so the newest
        # action on the list is this review.
        record(identity, "system.components_reviewed", "system/components", "operations")
        groups = [{"key": g, "title": title, "rows": [r for r in rows if r["group"] == g]}
                  for g, title in reg.GROUPS.items()]
        by_state = {
            "behind": [r for r in rows if r["state"] in ("behind", "drifted", "updating")],
            "unknown": [r for r in rows if r["state"] == "unknown"],
        }
        try:
            backups = [_backup_view(b, now) for b in (journal.backups() or [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("components: backups unavailable: %s", exc)
            backups = []
        try:
            releases = [_release_view(r) for r in (journal.releases() or [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("components: releases unavailable: %s", exc)
            releases = []
        return page(request, "components.html", identity, active="components",
                    rows=rows, groups=groups, cards=cards, by_state=by_state,
                    manifest=_manifest_line(ctx.manifest, now),
                    open_job=open_job,
                    job_view=_job_view(open_job, registry, now) if open_job else None,
                    backups=backups, releases=releases,
                    keep=reg.KEEP, cadence=reg.CADENCE_DAYS,
                    last_entry=_last_entry(journal, registry, now),
                    recent=_recent(reader, now),
                    can_update=identity.can("system:update"),
                    journal_persistent=getattr(journal, "persistent", True),
                    refresh=OPEN_JOB_REFRESH_SECONDS)

    @app.get("/system/components/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str,
                 identity: Identity = Depends(current_identity)):
        """A job's stable address lands on the row it is open on."""
        require(identity, "system:admin")
        job = _open_job_of(_journal(app), job_id)
        return RedirectResponse(f"/system/components/{job['component']}#job", status_code=303)

    @app.get("/system/components/{key}", response_class=HTMLResponse)
    def component_detail(request: Request, key: str,
                         identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        _component(_registry(), key)
        record(identity, "system.components_reviewed", f"system/components/{key}", "operations")
        return _detail(request, identity, key)

    # ---- acknowledgement ---------------------------------------------

    @app.post("/system/components/{key}/acknowledge", response_class=HTMLResponse)
    def component_acknowledge(request: Request, key: str,
                              until: str = Form(""), reason: str = Form(""),
                              identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        journal = _journal(app)
        _component(_registry(), key)
        reason = " ".join(reason.split())[:300]
        try:
            until_date = date.fromisoformat(until.strip())
        except ValueError:
            return _detail(request, identity, key, status_code=400,
                           error="An acknowledgement needs a date (YYYY-MM-DD) to keep the row as is until.")
        if until_date < date.today():
            return _detail(request, identity, key, status_code=400,
                           error="A date in the past keeps nothing; name a day still to come.")
        if not reason:
            return _detail(request, identity, key, status_code=400,
                           error="An acknowledgement needs a reason; \"keep as is\" without one is not a decision.")
        journal.acknowledge(key, until_date.isoformat(), reason, identity.username)
        _drain(journal, identity, ("system.component_acknowledged",
                                   f"components/{key} until={until_date.isoformat()}"))
        return RedirectResponse(f"/system/components/{key}#ack", status_code=303)

    # ---- the update job ----------------------------------------------

    @app.post("/system/components/{key}/update", response_class=HTMLResponse)
    async def component_update(request: Request, key: str,
                               identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        require(identity, "system:update")
        journal = _journal(app)
        component = _component(_registry(), key)
        form = await request.form()
        target = " ".join(str(form.get("target", "")).split())[:120]
        wanted = str(form.get("mode", "")).strip()
        if component.mode == "record":
            record(identity, "system.update_refused", f"components/{key} record-only", "operations")
            return _refused(request, identity, 400, "Record only",
                            f"{component.name} is record only: it has no apply. It changes "
                            "inside a release, and its row says how.")
        if not target:
            return _refused(request, identity, 400, "No target",
                            "Name the target: the release, version or digest the update moves to.")
        mode = "guided"
        if component.mode == "direct" and wanted != "guided" and _updater_reachable(app):
            mode = "direct"
        try:
            job = journal.start_job(key, target, identity.username, mode)
        except Exception as exc:  # noqa: BLE001 - the lock, whichever journal raised it
            if type(exc).__name__ != "JobOpen":
                raise
            held = journal.open_job() or {}
            record(identity, "system.update_refused", f"components/{key} job-open", "operations")
            return _refused(request, identity, 409, "One at a time",
                            f"An update of {held.get('component', 'another component')} is "
                            f"already open, held by {held.get('started_by', 'someone')} since "
                            f"{held.get('started_at', 'unknown')}. Finish it or roll it back "
                            "before starting another: the open job row is the lock.")
        _drain(journal, identity, ("system.update_started",
                                   f"components/{key} target={target} mode={job.get('mode', mode)}"))
        return RedirectResponse(f"/system/components/{key}#job", status_code=303)

    @app.post("/system/components/job/{job_id}/confirm", response_class=HTMLResponse)
    def job_confirm(request: Request, job_id: str, phrase: str = Form(""),
                    identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        require(identity, "system:update")
        journal = _journal(app)
        job = _open_job_of(journal, job_id)
        key = str(job["component"])
        # Step-up re-verification (proposal §8) is wired here the day the
        # login routes expose a helper that requires a recent second
        # factor; today they do not (core/web/login_routes.py has the MFA
        # flow at sign-in only), so the gate is the typed phrase naming the
        # component and the target, and nothing pretends otherwise.
        try:
            journal.confirm_plan(job_id, phrase.strip(), identity.username)
        except Exception as exc:  # noqa: BLE001 - shown on the row, audited as a refusal
            log.warning("components: plan not confirmed for %s: %s", key, exc)
            record(identity, "system.update_refused", f"components/{key} phrase", "operations")
            return _detail(request, identity, key, status_code=400,
                           error=f"Not confirmed: {exc}")
        _drain(journal, identity, ("system.update_confirmed", f"components/{key} job={job_id}"))
        return RedirectResponse(f"/system/components/{key}#job", status_code=303)

    @app.post("/system/components/job/{job_id}/step", response_class=HTMLResponse)
    async def job_step(request: Request, job_id: str,
                       identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        require(identity, "system:update")
        journal = _journal(app)
        job = _open_job_of(journal, job_id)
        key = str(job["component"])
        form = await request.form()
        step = str(form.get("step", "")).strip()
        outcome = str(form.get("outcome", "")).strip()
        attested = str(form.get("attested", "")).strip().lower() in ("1", "on", "true", "yes")
        evidence = " ".join(str(form.get("evidence", "")).split())[:2000] or None
        mode = str(form.get("mode", "")).strip() or None
        if step not in reg.STEPS or outcome not in OUTCOMES:
            return _detail(request, identity, key, status_code=400,
                           error="A step record names one of the five steps and an outcome of ok or failed.")
        try:
            job = journal.advance(job_id, step, outcome, identity.username,
                                  attested=attested, evidence=evidence, mode=mode)
        except Exception as exc:  # noqa: BLE001
            log.warning("components: step not recorded for %s: %s", key, exc)
            return _detail(request, identity, key, status_code=400,
                           error=f"Not recorded: {exc}")
        _drain(journal, identity, ("system.update_step",
                                   f"components/{key} {step}={outcome} mode={job.get('mode')}"
                                   + (" attested" if attested else "")))
        return RedirectResponse(f"/system/components/{key}#job", status_code=303)

    @app.post("/system/components/job/{job_id}/rollback", response_class=HTMLResponse)
    def job_rollback(request: Request, job_id: str,
                     identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        require(identity, "system:update")
        journal = _journal(app)
        key = str(_open_job_of(journal, job_id)["component"])
        try:
            journal.rollback(job_id, identity.username)
        except Exception as exc:  # noqa: BLE001
            return _detail(request, identity, key, status_code=400, error=f"Not rolled back: {exc}")
        _drain(journal, identity, ("system.update_rolled_back", f"components/{key} job={job_id}"))
        return RedirectResponse(f"/system/components/{key}#job", status_code=303)

    @app.post("/system/components/job/{job_id}/finish", response_class=HTMLResponse)
    def job_finish(request: Request, job_id: str,
                   identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        require(identity, "system:update")
        journal = _journal(app)
        key = str(_open_job_of(journal, job_id)["component"])
        try:
            journal.finish(job_id, identity.username)
        except Exception as exc:  # noqa: BLE001
            return _detail(request, identity, key, status_code=400, error=f"Not finished: {exc}")
        _drain(journal, identity, ("system.update_finished", f"components/{key} job={job_id}"))
        return RedirectResponse(f"/system/components/{key}#job", status_code=303)

    # ---- backups -----------------------------------------------------

    @app.post("/system/components/backups/{store}/rehearsed", response_class=HTMLResponse)
    def backup_rehearsed(request: Request, store: str,
                         identity: Identity = Depends(current_identity)):
        require(identity, "system:admin")
        require(identity, "system:update")
        journal = _journal(app)
        try:
            stores = {store_slug(str(b.get("store") or b.get("name") or "")): b
                      for b in (journal.backups() or [])}
        except Exception as exc:  # noqa: BLE001
            log.warning("components: backups unavailable: %s", exc)
            stores = {}
        row = stores.get(store)
        if row is None:
            raise HTTPException(status_code=404, detail="No such store: the backups table lists "
                                                        "every store the journal records.")
        name = str(row.get("store") or row.get("name"))
        journal.rehearsed(name, identity.username)
        _drain(journal, identity, ("system.restore_rehearsed", f"backups/{store}"))
        return RedirectResponse("/system/components#backups", status_code=303)
# Made by Ryan Gomez & Co. Inc.
