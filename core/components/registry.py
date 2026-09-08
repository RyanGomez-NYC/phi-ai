# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The component registry: THE CONTRACT between the readers (members.py),
the engine (journal.py), the platform screen (core/web/components_routes.py)
and any mirror that pins itself to this module in its own tests (the
demonstration's does).

One interface, declared per component, enumerated by the screen:

  key           stable identifier, snake_case; the demo mirror uses the same
  group         A..E (GROUPS); the screen renders one table per group
  name, kind    what the row is, in words
  mode          how the platform can update it: direct | guided | record
  backup_unit   what is taken before an apply, in words
  recovery      the way back, in one sentence
  cadence_days  how often "latest known" must be refreshed, or None
  read(ctx)     -> Reading: running / built / latest, each a Fact with its
                   source and when it was checked; the state; the evidence
  procedure(ctx)-> the five Steps in this component's mode

States (the five the screen shows):
  current   running == built == latest, all checked
  behind    latest known is newer than what is built
  drifted   running differs from built (the deployed copy is not the built copy)
  unknown   a fact could not be read, or was never checked - amber, never green
  updating  a job is open on this component

Never assert unverified state: a Fact with checked_at None is unknown,
whatever its value says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Vocabulary. Spelled once; the demo mirror is pinned to these strings.
# ---------------------------------------------------------------------------

GROUPS: dict[str, str] = {
    "A": "Code and build",
    "B": "Dependencies and runtimes",
    "C": "Reference data on someone else's cadence",
    "D": "State, schema, secrets",
    "E": "Verification and recovery",
}

MODES: tuple[str, ...] = ("direct", "guided", "record")
STATES: tuple[str, ...] = ("current", "behind", "drifted", "unknown", "updating")
STEPS: tuple[str, ...] = ("Plan", "Back up", "Apply", "Verify", "Recover")

# Ryan's numbers (proposal §14, decision 4). One place; the screen reads
# them from here and the CLI writes them into the manifest.
CADENCE_DAYS: dict[str, int] = {
    "vendor_docs": 90, "advisories": 14, "keys": 365, "rehearsal": 180,
}
KEEP: dict[str, int] = {"images": 3, "backups": 3}


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Facts and evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    """One thing the screen says, with where it came from and when it was
    checked. A Fact whose checked_at is None is UNKNOWN whatever its value:
    the screen colours it amber and the state logic treats it as unread."""

    value: str
    source: str                       # e.g. "BUILD.json", "git rev-parse HEAD", "manifest: pypi"
    checked_at: Optional[datetime] = None
    href: Optional[str] = None        # a link for the source, when there is one

    @property
    def known(self) -> bool:
        return self.checked_at is not None and self.value not in ("", "unknown")

    @staticmethod
    def unknown(source: str, why: str = "unknown") -> "Fact":
        return Fact(value=why, source=source, checked_at=None)


@dataclass(frozen=True)
class Evidence:
    """The rows behind a reading - every total opens to its rows."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class Step:
    """One of the five steps, in the mode the component runs in.

    direct_capable  the updater can perform this step itself
    instruction     what a person does in guided mode: the exact command,
                    SQL or file, copyable; empty when the platform does it
    verify          how the screen verifies the step by machine, in words;
                    "attest" when it cannot and the admin attests instead
    runbook         the runbook section the instruction was generated from
    """

    name: str
    direct_capable: bool
    instruction: str = ""
    verify: str = "attest"
    runbook: str = ""

    def __post_init__(self) -> None:
        if self.name not in STEPS:
            raise ValueError(f"not one of the five steps: {self.name!r}")


@dataclass
class Reading:
    """What one component looks like right now."""

    key: str
    running: Fact
    built: Fact
    latest: Fact
    state: str
    evidence: Evidence = field(default_factory=lambda: Evidence(columns=()))
    note: str = ""                      # one plain sentence when the state needs one
    checked_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"not one of the five states: {self.state!r}")
        if self.checked_at is None:
            stamps = [f.checked_at for f in (self.running, self.built, self.latest) if f.checked_at]
            self.checked_at = min(stamps) if stamps else None


def decide_state(running: Fact, built: Fact, latest: Fact, *, updating: bool = False,
                 newer: Optional[Callable[[str, str], bool]] = None) -> str:
    """The one place the state is decided from the three facts.

    `newer(latest, built)` says whether latest is newer than built; when not
    given, inequality is the test. Unknown beats everything except updating:
    a fact that was never checked cannot make a component current."""
    if updating:
        return "updating"
    if not (running.known and built.known):
        return "unknown"
    if running.value != built.value:
        return "drifted"
    if not latest.known:
        return "unknown"
    is_newer = newer(latest.value, built.value) if newer else latest.value != built.value
    return "behind" if is_newer else "current"


# ---------------------------------------------------------------------------
# The readers' context
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """Everything a reader may consult. Nothing here reaches the network:
    a PHI host never phones home, so "latest known" comes from the manifest
    the workstation CLI wrote (core/components/manifest.py)."""

    root: Path                                   # repository / install root
    connect: Optional[Callable[[], Any]] = None  # index DB connection factory, or None
    platform_state: Any = None                   # core.web.platform_state.PlatformState, or None
    manifest: Optional[dict] = None              # components.manifest.json, parsed, or None
    build: Optional[dict] = None                 # BUILD.json, parsed, or None
    checked_at: datetime = field(default_factory=now)
    open_job_key: Optional[str] = None           # component key of the open update job, if any
    journal: Any = None                          # core.components.journal.Journal, or None (group E rows read it)

    def manifest_fact(self, key: str, field_name: str = "value") -> Fact:
        """A "latest known" fact from the manifest, or unknown with the reason."""
        if not self.manifest:
            return Fact.unknown("manifest", "no manifest - run scripts/components.py check")
        entry = (self.manifest.get("components") or {}).get(key)
        if not entry:
            return Fact.unknown("manifest", "not in the manifest")
        checked = entry.get("fetched_at") or self.manifest.get("produced_at")
        try:
            stamp = datetime.fromisoformat(str(checked).replace("Z", "+00:00")) if checked else None
        except ValueError:
            stamp = None
        return Fact(value=str(entry.get(field_name, "")), source="manifest: " + str(entry.get("source", "")),
                    checked_at=stamp, href=entry.get("url"))


# ---------------------------------------------------------------------------
# The component interface and the registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Component:
    key: str
    group: str
    name: str
    kind: str
    mode: str
    backup_unit: str
    recovery: str
    cadence_days: Optional[int]
    reader: Callable[[Context], Reading]
    procedure: Callable[[Context], tuple[Step, ...]]
    demo_only: bool = False        # a row the demo shows that the platform has no equivalent of

    def __post_init__(self) -> None:
        if self.group not in GROUPS:
            raise ValueError(f"unknown group {self.group!r} for {self.key}")
        if self.mode not in MODES:
            raise ValueError(f"unknown mode {self.mode!r} for {self.key}")

    def read(self, ctx: Context) -> Reading:
        """Never raises: a reader that fails yields an unknown reading that
        says why, because a screen that 500s on one missing file is a screen
        nobody trusts to tell the truth about the rest."""
        try:
            reading = self.reader(ctx)
        except Exception as exc:  # noqa: BLE001 - the whole point is to keep reading
            why = f"could not read: {type(exc).__name__}: {exc}"[:160]
            reading = Reading(key=self.key, running=Fact.unknown(self.kind, why),
                              built=Fact.unknown(self.kind, why), latest=Fact.unknown(self.kind, why),
                              state="unknown", note=why)
        if ctx.open_job_key == self.key:
            reading.state = "updating"
        return reading

    def steps(self, ctx: Context) -> tuple[Step, ...]:
        steps = tuple(self.procedure(ctx))
        names = tuple(s.name for s in steps)
        if names != STEPS:
            raise ValueError(f"{self.key}: a procedure is the five steps in order, got {names}")
        if self.mode == "record" and any(s.direct_capable for s in steps):
            raise ValueError(f"{self.key}: a record-only component has no direct step")
        return steps


_REGISTRY: dict[str, Component] = {}


def component(**decl):
    """Declare a component. Usage in members.py:

        @component(key="release", group="A", name="Release stamp", kind="...",
                   mode="record", backup_unit="...", recovery="...", cadence_days=None,
                   procedure=lambda ctx: (...))
        def read_release(ctx: Context) -> Reading: ...

    The decorated function is the reader. Keys are unique; declaring one
    twice is a programming error, not an override."""
    def wrap(reader: Callable[[Context], Reading]) -> Callable[[Context], Reading]:
        key = decl["key"]
        if key in _REGISTRY:
            raise ValueError(f"component {key!r} declared twice")
        _REGISTRY[key] = Component(reader=reader, **decl)
        return reader
    return wrap


def registry() -> dict[str, Component]:
    """Every declared component, in declaration (group) order. Importing
    members.py is what populates it; this accessor never imports on the
    caller's behalf, so tests can register fakes."""
    return dict(_REGISTRY)


def load_members() -> dict[str, Component]:
    """Import the real declarations, once, and return the registry."""
    from core.components import members  # noqa: F401  (registration side effect)
    return registry()


def read_all(ctx: Context, reg: Optional[dict[str, Component]] = None) -> list[tuple[Component, Reading]]:
    reg = reg if reg is not None else registry()
    return [(c, c.read(ctx)) for c in reg.values() if not c.demo_only]


def summary(readings: list[tuple[Component, Reading]]) -> dict[str, int]:
    """The three cards: current, behind-or-drifted, unknown (updating counts
    with behind-or-drifted: something is being done about it)."""
    out = {"current": 0, "behind": 0, "unknown": 0}
    for _, r in readings:
        if r.state == "current":
            out["current"] += 1
        elif r.state == "unknown":
            out["unknown"] += 1
        else:
            out["behind"] += 1
    return out
