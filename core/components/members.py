# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Every component, declared once, in group order A to E.

Each declaration is the registry interface of core/components/registry.py:
key, group, name, kind and mode - a mirror elsewhere may pin itself to
these declarations in its own tests - plus the reader that turns real
evidence into a Reading, and the five-step procedure in the row's mode.

Readers never raise on a missing file: a fact that cannot be read is an
unknown Fact that says why. Nothing here reaches the network; "latest
known" comes from the manifest the workstation CLI wrote, and an entry
written offline is unknown for any fact that needs upstream.

Guided instructions are the exact command, SQL or file, copyable. Each
step's verify text says how the screen verifies it by machine, or
"attest" when the admin attests instead.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core.components import build, ledger, manifest, vendored
from core.components.registry import (
    CADENCE_DAYS, KEEP, STEPS, Context, Evidence, Fact, Reading, Step,
    component, decide_state, now,
)
from core.config.settings import env_var

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def context(root: Path, *, connect: Optional[Callable[[], Any]] = None, platform_state: Any = None,
            journal: Any = None, open_job_key: Optional[str] = None) -> Context:
    """A readers' context with the manifest and the build stamp loaded from
    the root, and the open job's component looked up in the journal."""
    root = Path(root)
    if open_job_key is None and journal is not None:
        try:
            job = journal.open_job()
        except Exception:  # noqa: BLE001 - the screen still reads
            job = None
        open_job_key = job["component"] if job else None
    return Context(root=root, connect=connect, platform_state=platform_state,
                   manifest=manifest.load(root), build=build.read_stamp(root),
                   open_job_key=open_job_key, journal=journal)


def _read(root: Path, rel: str) -> Optional[str]:
    try:
        return (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _json_file(root: Path, rel: str) -> Optional[dict]:
    text = _read(root, rel)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _fact(value: Any, source: str, ctx: Context, href: Optional[str] = None) -> Fact:
    return Fact(value=str(value), source=source, checked_at=ctx.checked_at, href=href)


def _same(fact: Fact, source: str) -> Fact:
    """The same value under another source: for a component with no built
    copy, running and built are the one record."""
    return Fact(value=fact.value, source=source, checked_at=fact.checked_at)


def _latest(ctx: Context, key: str, *, needs_network: bool) -> Fact:
    """"Latest known" from the manifest. An entry the workstation wrote
    offline says nothing about upstream: unknown, with the reason."""
    entry = (ctx.manifest or {}).get("components", {}).get(key) if ctx.manifest else None
    if needs_network and entry is not None and manifest.is_offline(entry):
        return Fact.unknown("manifest: " + str(entry.get("source", "")),
                            "upstream not checked: the manifest was produced offline "
                            "(scripts/components.py check without --offline is slice 4)")
    return ctx.manifest_fact(key)


def _manifest_entry(ctx: Context, key: str) -> dict:
    return ((ctx.manifest or {}).get("components", {}) or {}).get(key) or {}


def _upstream_newer(entry: dict) -> bool:
    """Whether an online manifest entry says upstream has moved past what
    is built: it lists what is behind, an advisory, or a deprecation. A
    descriptive value alone never decides it - the lists do."""
    return bool(entry.get("behind") or entry.get("advisories") or entry.get("deprecated"))


def _reading(ctx: Context, key: str, running: Fact, built: Fact, latest: Fact, *,
             columns: tuple[str, ...], rows: list[tuple], note: str = "",
             newer: Optional[Callable[[str, str], bool]] = None) -> Reading:
    state = decide_state(running, built, latest, newer=newer)
    return Reading(key=key, running=running, built=built, latest=latest, state=state,
                   evidence=Evidence(columns=columns, rows=tuple(tuple(str(c) for c in r) for r in rows)),
                   note=note)


def _steps(mode: str, rows: list[tuple[str, str, str]]) -> tuple[Step, ...]:
    """Five Steps from (instruction, verify, runbook) rows, in the mode."""
    direct = mode == "direct"
    return tuple(Step(name=name, direct_capable=direct, instruction=instr, verify=verify, runbook=runbook)
                 for name, (instr, verify, runbook) in zip(STEPS, rows))


def _changelog_top(root: Path) -> Optional[tuple[str, str, str]]:
    text = _read(root, "CHANGELOG.md")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            m = re.search(r"\d{4}-\d{2}-\d{2}", heading)
            return (heading.split()[0] if heading else "", m.group(0) if m else "", heading)
    return None


def _git_tags(root: Path) -> Optional[list[tuple[str, str, str]]]:
    out = build.git(root, "for-each-ref",
                    "--format=%(refname:short)\t%(creatordate:short)\t%(objectname)\t%(*objectname)", "refs/tags")
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        name, day, obj, peeled = (line.split("\t") + ["", "", "", ""])[:4]
        rows.append((name, day, peeled or obj))
    return rows


def _tag_names_release(tags: list[tuple[str, str, str]], release: str) -> Optional[tuple[str, str, str]]:
    major_minor = ".".join(release.split(".")[:2])
    for t in tags:
        if t[0] in (release, major_minor):
            return t
    return None


def _lock_pins(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in re.finditer(
        r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)(?:\[[^\]]*\])?==([^\s\;]+)", text, re.M)]


def _installed(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _dockerfile_from(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    m = re.search(r"^FROM\s+(\S+)", text, re.M)
    return m.group(1) if m else None


def _tf_pins(text: str) -> tuple[Optional[str], list[tuple[str, str, str]]]:
    """(required_version, [(provider, source, constraint)]) from versions.tf."""
    req = re.search(r'required_version\s*=\s*"([^"]+)"', text)
    providers = []
    start = text.find("required_providers")
    if start >= 0:
        i = text.find("{", start)
        depth, block = 0, ""
        for j in range(i, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                block = text[i + 1:j]
                break
        for name, body in re.findall(r"(\w+)\s*=\s*\{([^}]*)\}", block):
            src = re.search(r'source\s*=\s*"([^"]+)"', body)
            ver = re.search(r'version\s*=\s*"([^"]+)"', body)
            providers.append((name, src.group(1) if src else "", ver.group(1) if ver else ""))
    return (req.group(1) if req else None), sorted(providers)


def _tf_lock(text: str) -> dict[str, str]:
    """provider short name -> selected version from .terraform.lock.hcl."""
    out = {}
    for src, body in re.findall(r'provider\s+"([^"]+)"\s*\{(.*?)\n\}', text, re.S):
        ver = re.search(r'version\s*=\s*"([^"]+)"', body)
        if ver:
            out[src.rsplit("/", 1)[-1]] = ver.group(1)
    return out


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _satisfies(version: str, constraint: str) -> Optional[bool]:
    """Terraform's `~>` and comparison operators; None when the constraint
    uses something else (the screen then says it could not decide)."""
    ok = True
    for op, val in re.findall(r"(~>|>=|<=|>|<|=)\s*([\d.]+)", constraint):
        v, c = _vtuple(version), _vtuple(val)
        if op == "~>":
            floor = c
            ceiling = c[:-1] if len(c) > 1 else c
            ceiling = ceiling[:-1] + (ceiling[-1] + 1,)
            ok = ok and v >= floor and v < ceiling
        elif op == ">=":
            ok = ok and v >= c
        elif op == "<=":
            ok = ok and v <= c
        elif op == ">":
            ok = ok and v > c
        elif op == "<":
            ok = ok and v < c
        elif op == "=":
            ok = ok and v[:len(c)] == c
    if not re.search(r"(~>|>=|<=|>|<|=)", constraint):
        return None
    return ok


def _journal(ctx: Context) -> Any:
    if ctx.journal is not None:
        return ctx.journal
    from core.components.journal import Journal
    return Journal(ctx.connect, root=ctx.root, platform_state=ctx.platform_state)


def _parse_stamp(value: str) -> Optional[datetime]:
    return manifest.parse_time(value)


def _days_since(stamp: Optional[datetime], at: datetime) -> Optional[int]:
    return None if stamp is None else (at - stamp).days


def _with_conn(ctx: Context, fn: Callable[[Any], Any]) -> tuple[Any, Optional[str]]:
    """Run fn(conn) on a fresh connection; (result, None) or (None, why)."""
    if ctx.connect is None:
        return None, "no index connection"
    try:
        conn = ctx.connect()
    except Exception as exc:  # noqa: BLE001
        return None, f"index connection failed: {type(exc).__name__}"
    try:
        return fn(conn), None
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None, f"query failed: {type(exc).__name__}: {str(exc)[:120]}"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


IMAGE_RUNBOOK = "runbooks/RUNBOOK_INSTALL.md"
INDEX_RUNBOOK = "runbooks/RUNBOOK_INDEX_MAINTENANCE.md"
AWS_RUNBOOK = "runbooks/RUNBOOK_AWS_SETUP.md"
EMR_RUNBOOK = "docs/EMR_CONNECTORS.md"
MODEL_RUNBOOK = "runbooks/RUNBOOK_MODEL_GOVERNANCE.md"
RETENTION_RUNBOOK = "runbooks/RUNBOOK_RETENTION_RULES.md"
VERIFY_RUNBOOK = "runbooks/RUNBOOK_VERIFICATION.md"
RELEASE_CHECKLIST = "docs/RELEASE_CHECKLIST.md"
NOTHING = "Nothing to do: this row records; it is not updated here."


# ---------------------------------------------------------------------------
# A. Code and build
# ---------------------------------------------------------------------------

def _release_procedure(ctx: Context) -> tuple[Step, ...]:
    rel = build.read_release(ctx.root) or "unknown"
    return _steps("record", [
        ("", "The RELEASE file, CHANGELOG.md's top heading, __version__ and the tag are read and compared; "
             "the target release is named and the admin confirms it by typing the component and the version.",
         RELEASE_CHECKLIST),
        ("", f"The previous release stays kept ({KEEP['images']} images retained); the journal records "
             "which digest is available to return to.", RELEASE_CHECKLIST),
        ("", "The stamp changes only inside a release: the apply is the Running image row's; nothing writes "
             "a stamp on a running host.", RELEASE_CHECKLIST),
        ("", f"The colophon, the healthcheck and BUILD.json all print {rel}; a disagreement fails the step.",
         RELEASE_CHECKLIST),
        ("", "Roll back to the previous release through the Running image row; the stamp rolls back with it "
             "and the verify sweep runs again before recovered is shown.", RELEASE_CHECKLIST),
    ])


@component(key="release", group="A", name="Release stamp", kind="release stamp", mode="record",
           backup_unit=f"The previous release, kept ({KEEP['images']} images retained).",
           recovery="Roll the release back: bring up the previous image digest and run the verify sweep.",
           cadence_days=None, procedure=_release_procedure)
def read_release(ctx: Context) -> Reading:
    import core

    running = _fact(core.__version__, "core/__init__.py __version__ (read from RELEASE)", ctx)
    rel = build.read_release(ctx.root)
    built = _fact(rel, "RELEASE", ctx) if rel else Fact.unknown("RELEASE", "no RELEASE file at the root")
    top = _changelog_top(ctx.root)
    latest = (_fact(top[0], "CHANGELOG.md top heading", ctx) if top and top[0]
              else Fact.unknown("CHANGELOG.md", "no '## ' heading in CHANGELOG.md"))
    rows = [("RELEASE", rel or "absent", "RELEASE"),
            ("core/__init__.py __version__", core.__version__, "core/__init__.py"),
            ("CHANGELOG.md top heading", top[2] if top else "absent", "CHANGELOG.md")]
    note = ""
    tags = _git_tags(ctx.root)
    if tags is None:
        rows.append(("tag", "unknown: no git checkout", "git for-each-ref refs/tags"))
        note = "no git checkout: whether a tag names the release cannot be read here"
    else:
        named = _tag_names_release(tags, rel or "") if rel else None
        if named:
            rows.append(("tag naming the release", f"{named[0]} at {build.short(named[2])} ({named[1]})",
                         "git for-each-ref refs/tags"))
        else:
            rows.append(("tag naming the release", "none", "git for-each-ref refs/tags"))
            note = f"no tag equals {rel} or its major.minor"
        for name, day, sha in tags:
            rows.append((f"tag {name}", f"{build.short(sha)} ({day})", "git for-each-ref refs/tags"))
    return _reading(ctx, "release", running, built, latest, columns=("Stamp", "Value", "Source"),
                    rows=rows, note=note)


def _build_stamp_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The stamp the release will carry - release, commit, tree, build date, image digest - is shown "
             "beside the stamp that is running; the admin confirms.", RELEASE_CHECKLIST),
        ("", "The running stamp and its digest are recorded as the point of return; the previous image stays kept.",
         RELEASE_CHECKLIST),
        ("", "The stamp is written by scripts/components.py build into BUILD.json inside the image; the apply "
             "is the image update.", RELEASE_CHECKLIST),
        ("", "The running image reports the stamp the manifest names, field for field, and the healthcheck is green.",
         RELEASE_CHECKLIST),
        ("", "Bring the previous digest up; its stamp is the proof the rollback landed, and the sweep runs again.",
         RELEASE_CHECKLIST),
    ])


@component(key="build_stamp", group="A", name="Build stamp", kind="BUILD.json", mode="record",
           backup_unit=f"The previous release, kept ({KEEP['images']} images retained).",
           recovery="Roll the release back; the stamp of the previous image comes back with it.",
           cadence_days=None, procedure=_build_stamp_procedure)
def read_build_stamp(ctx: Context) -> Reading:
    stamp = ctx.build if ctx.build is not None else build.read_stamp(ctx.root)
    if stamp:
        built = _fact(f"{stamp.get('release', 'unknown')} @ {build.short(stamp.get('commit'))}", "BUILD.json", ctx)
    else:
        built = Fact.unknown("BUILD.json", "no BUILD.json: run scripts/components.py build")
    facts = build.git_facts(ctx.root)
    rel = build.read_release(ctx.root) or "unknown"
    if build.has_checkout(ctx.root) and facts["commit"] != build.UNKNOWN:
        running = _fact(f"{rel} @ {build.short(facts['commit'])}", "RELEASE + git rev-parse HEAD", ctx)
    elif stamp:
        running = Fact(value=built.value, source="BUILD.json (no checkout: the stamp inside the image is the running copy)",
                       checked_at=ctx.checked_at)
    else:
        running = Fact.unknown("git", "no BUILD.json and no git checkout: what is running cannot be named")
    latest = _latest(ctx, "build_stamp", needs_network=False)
    rows = []
    for field in ("release", "commit", "branch", "built_at", "tree_sha", "image_digest"):
        rows.append((field, str(stamp.get(field, "absent")) if stamp else "no BUILD.json", "BUILD.json"))
    rows.append(("git HEAD", facts["commit"], facts["source"]))
    rows.append(("git branch", facts["branch"], facts["source"]))
    rows.append(("git tree", facts["tree_sha"], facts["source"]))
    return _reading(ctx, "build_stamp", running, built, latest, columns=("Field", "Value", "Source"), rows=rows)


def _compose_services(text: Optional[str]) -> list[tuple[str, str, str, str]]:
    if text is None:
        return []
    services, cur, in_services = [], None, False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line):
            in_services = False
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            cur = {"name": m.group(1), "image": "", "build": "", "restart": "", "profiles": ""}
            services.append(cur)
            continue
        if cur is None:
            continue
        for key, pat in (("image", r"^    image:\s*(\S+)"), ("restart", r"^    restart:\s*(\S+)"),
                         ("build", r"^      dockerfile:\s*(\S+)"), ("profiles", r"^    profiles:\s*\[([^\]]*)\]")):
            m = re.match(pat, line)
            if m:
                cur[key] = m.group(1).replace('"', "").strip()
        if re.match(r"^    build:\s*$", line) and not cur["build"]:
            cur["build"] = "Dockerfile"
    def image_or_build(s: dict) -> str:
        m = re.match(r"\$\{PHI_AI_IMAGE:-([^}]+)\}$", s["image"])
        if m:   # the release unit: pinned by PHI_AI_IMAGE, built for development
            return f"PHI_AI_IMAGE, default {m.group(1)} (build: {s['build'] or 'Dockerfile'})"
        return s["image"] or f"built from {s['build'] or 'Dockerfile'}"
    return [(s["name"], image_or_build(s), s["restart"] or "no",
             s["profiles"] or "always") for s in sorted(services, key=lambda s: s["name"])]


def _running_image_procedure(ctx: Context) -> tuple[Step, ...]:
    root = ctx.root
    return _steps("direct", [
        ("scripts/components.py build --image-digest <repo@sha256:...> --sign <keyfile>   # on the workstation; "
         "the digest is the one the registry returned on push (docker push prints it), never docker inspect's "
         "RepoDigests; copy BUILD.json, MANIFEST.sha256 and MANIFEST.sha256.sig to <deployment>/releases/",
         "The updater verifies MANIFEST.sha256.sig against the host's config/release_signing.pub (placed by the "
         "operator, never in the repository) and that BUILD.json names "
         "the target digest; the admin confirms by typing 'running_image <digest>'.", IMAGE_RUNBOOK),
        (f"grep '^PHI_AI_IMAGE=' {root / '.env'}   # the previous digest is the point of return; record it",
         "The journal records the previous digest from the env file; no recorded digest, no apply.", IMAGE_RUNBOOK),
        (f"docker pull <repo@sha256:...> && sed -i 's|^PHI_AI_IMAGE=.*|PHI_AI_IMAGE=<repo@sha256:...>|' "
         f"{root / '.env'} && docker compose -f {root / 'docker-compose.yml'} up -d",
         "The updater pulls the digest and runs compose up -d; the web app serves the maintenance banner while "
         "the job is open.", IMAGE_RUNBOOK),
        (f"docker compose -f {root / 'docker-compose.yml'} exec -T web python -m core.healthcheck",
         "python -m core.healthcheck exits 0 inside the web container; the running digest equals the target.",
         IMAGE_RUNBOOK),
        (f"sed -i 's|^PHI_AI_IMAGE=.*|PHI_AI_IMAGE=<previous digest>|' {root / '.env'} && "
         f"docker compose -f {root / 'docker-compose.yml'} up -d && docker compose -f {root / 'docker-compose.yml'} "
         "exec -T web python -m core.healthcheck",
         "The previous digest is up and the healthcheck is green again; recovered shows only after that second pass.",
         IMAGE_RUNBOOK),
    ])


def _compose_default_image(text: Optional[str]) -> str:
    """The tag compose falls back to when PHI_AI_IMAGE is unset: the
    `image: ${PHI_AI_IMAGE:-<tag>}` line the release unit puts on every
    Dockerfile-built service. Empty when the compose file has no such line."""
    m = re.search(r"\$\{PHI_AI_IMAGE:-([^}]+)\}", text or "")
    return m.group(1).strip() if m else ""


@component(key="running_image", group="A", name="Running image", kind="compose services", mode="direct",
           backup_unit=f"The previous image digest, kept ({KEEP['images']} retained).",
           recovery="compose up -d the previous digest, then the verify sweep.",
           cadence_days=None, procedure=_running_image_procedure)
def read_running_image(ctx: Context) -> Reading:
    digest_env = os.environ.get("PHI_AI_IMAGE", "").strip()
    compose_text = _read(ctx.root, "docker-compose.yml")
    default_tag = _compose_default_image(compose_text)
    if digest_env:
        running = _fact(digest_env, "environment PHI_AI_IMAGE (from .env)", ctx)
    elif default_tag:
        running = Fact.unknown("environment PHI_AI_IMAGE",
                               f"PHI_AI_IMAGE is not set: compose runs its default tag {default_tag}, built from "
                               "the Dockerfile, not a pinned digest")
    else:
        running = Fact.unknown("environment PHI_AI_IMAGE",
                               "PHI_AI_IMAGE is not set and the compose services carry no image: line: built from "
                               "the Dockerfile, not pinned to a digest")
    stamp = ctx.build if ctx.build is not None else build.read_stamp(ctx.root)
    if not stamp:
        built = Fact.unknown("BUILD.json", "no BUILD.json: unknown without a stamp")
    elif not stamp.get("image_digest"):
        built = Fact.unknown("BUILD.json", "BUILD.json carries no image_digest (built without --image-digest)")
    else:
        built = _fact(stamp["image_digest"], "BUILD.json image_digest", ctx)
    latest = _latest(ctx, "running_image", needs_network=False)
    rows = _compose_services(compose_text)
    note = ""
    if not rows:
        note = "no docker-compose.yml under the root"
    return _reading(ctx, "running_image", running, built, latest,
                    columns=("Service", "Image or build", "Restart", "Profile"), rows=rows, note=note)


# A tree may carry a row of its own here, at this point in group A - the
# demonstration records its payload's manifest, for one. Importing the
# module registers it in this order; a tree without it registers nothing.
try:
    from core.components import members_demo  # noqa: E402,F401
except ImportError:
    pass


# ---------------------------------------------------------------------------
# B. Dependencies and runtimes
# ---------------------------------------------------------------------------

def _python_pins_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The lock's pins are compared with the set installed inside the image and with the advisory "
             "manifest; the plan names every pin that changes and the image release that carries it.",
         RELEASE_CHECKLIST),
        ("", "The previous image digest stays kept; nothing is pip-installed into a running container, ever.",
         RELEASE_CHECKLIST),
        ("", "pip-compile --generate-hashes --strip-extras --output-file=requirements.lock requirements.txt "
             "on the workstation under Python 3.12, then build and sign the release image and ship it through "
             "the Running image row.", RELEASE_CHECKLIST),
        ("", "The installed set inside the new image equals the lock (importlib.metadata against "
             "requirements.lock), and the advisory manifest lists nothing against it.", RELEASE_CHECKLIST),
        ("", "Run the previous image; the pins roll back with it.", RELEASE_CHECKLIST),
    ])


@component(key="python_pins", group="B", name="Python pins", kind="dependency lock", mode="record",
           backup_unit="The previous image.", recovery="The previous image.",
           cadence_days=CADENCE_DAYS["advisories"], procedure=_python_pins_procedure)
def read_python_pins(ctx: Context) -> Reading:
    text = _read(ctx.root, "requirements.lock")
    if text is None:
        unknown = Fact.unknown("requirements.lock", "no requirements.lock under the root")
        return _reading(ctx, "python_pins", unknown, unknown, _latest(ctx, "python_pins", needs_network=True),
                        columns=("Package", "Pinned", "Installed", "State"), rows=[])
    pins = _lock_pins(text)
    lock_sha = vendored.sha256_file(ctx.root / "requirements.lock")
    built = _fact(f"{len(pins)} pins as locked", f"requirements.lock (sha256 {lock_sha[:12]})", ctx)
    rows, differ, absent = [], 0, 0
    for name, version in pins:
        inst = _installed(name)
        if inst is None:
            state, absent = "not installed", absent + 1
        elif inst == version:
            state = "as locked"
        else:
            state, differ = "differs", differ + 1
        rows.append((name, version, inst or "absent", state))
    if differ == 0 and absent == 0:
        running = _fact(built.value, "importlib.metadata against requirements.lock", ctx)
    else:
        running = _fact(f"{differ} of {len(pins)} pins differ from the lock; {absent} not installed",
                        "importlib.metadata against requirements.lock", ctx)
    entry = _manifest_entry(ctx, "python_pins")
    latest = _latest(ctx, "python_pins", needs_network=True)

    def newer(_latest: str, _built: str) -> bool:
        return _upstream_newer(entry) or (entry.get("lock_sha256") not in (None, "", lock_sha))

    note = ""
    if differ or absent:
        note = (f"this process runs python {platform.python_version()}: {differ} pins differ and {absent} are absent "
                "against the lock (the image installs the lock with --require-hashes)")
    return _reading(ctx, "python_pins", running, built, latest,
                    columns=("Package", "Pinned", "Installed", "State"), rows=rows, note=note, newer=newer)


def _runtimes_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The Python of the image and the Postgres engine of the index are read and shown; nothing to confirm.",
         AWS_RUNBOOK),
        ("", "Nothing is backed up here; the image and the engine each have their own row.", AWS_RUNBOOK),
        ("", "Nothing is applied; the base image moves inside a release and the engine version through the "
             "Infra pins checklist.", AWS_RUNBOOK),
        ("", "The running Python matches the Dockerfile's tag and the index reports the engine version rds.tf names.",
         AWS_RUNBOOK),
        ("", "Nothing is recovered here; the image and the engine rows carry their own recovery.", AWS_RUNBOOK),
    ])


@component(key="runtimes", group="B", name="Runtimes", kind="runtimes", mode="record",
           backup_unit="n/a: facts only.",
           recovery="n/a: a runtime changes through a release (the image) or a guided infra apply (the engine version).",
           cadence_days=None, procedure=_runtimes_procedure)
def read_runtimes(ctx: Context) -> Reading:
    running_py = ".".join(platform.python_version().split(".")[:2])
    running = _fact(f"python {running_py}", f"sys.version ({platform.python_version()})", ctx)
    base = _dockerfile_from(_read(ctx.root, "Dockerfile"))
    m = re.search(r"python:(\d+\.\d+)", base or "")
    built = (_fact(f"python {m.group(1)}", f"Dockerfile FROM {base}", ctx) if m
             else Fact.unknown("Dockerfile", "no FROM python:<major.minor> in the Dockerfile" if base else "no Dockerfile"))
    latest = _latest(ctx, "runtimes", needs_network=True)
    rows = [("python (this process)", platform.python_version(), "sys.version"),
            ("base image", base or "absent", "Dockerfile FROM")]
    rds = _read(ctx.root, "deploy/aws/rds.tf")
    if rds:
        eng = re.search(r'^\s*engine\s*=\s*"([^"]+)"', rds, re.M)
        engv = re.search(r'^\s*engine_version\s*=\s*"([^"]+)"', rds, re.M)
        rows.append(("index engine (declared)", f"{eng.group(1) if eng else 'unknown'} {engv.group(1) if engv else ''}".strip(),
                     "deploy/aws/rds.tf"))
    else:
        rows.append(("index engine (declared)", "unknown: no deploy/aws/rds.tf", "deploy/aws/rds.tf"))

    def version(conn):
        cur = conn.cursor()
        try:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            return str(row[0]) if row else "unknown"
        finally:
            cur.close()

    served, why = _with_conn(ctx, version)
    rows.append(("index server", served if served else f"unknown: {why}", "SELECT version()"))
    entry = _manifest_entry(ctx, "runtimes")
    return _reading(ctx, "runtimes", running, built, latest, columns=("Runtime", "Value", "Source"), rows=rows,
                    newer=lambda _l, _b: _upstream_newer(entry))


def _vendored_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "Each vendored file's recorded version and hash are compared with the upstream the manifest checked; "
             "the plan names the files a release would replace.", RELEASE_CHECKLIST),
        ("", "The previous release stays kept; vendored files change only inside a release.", RELEASE_CHECKLIST),
        ("", "Replace the files on the workstation, run scripts/components.py vendored to record version and hash "
             "per file, build and sign the release.", RELEASE_CHECKLIST),
        ("", "Every served file hashes to what VENDORED.json records, and the pages that load them render.",
         RELEASE_CHECKLIST),
        ("", "The previous release; its files come back with it.", RELEASE_CHECKLIST),
    ])


@component(key="vendored_frontend", group="B", name="Vendored front-end", kind="vendored files", mode="record",
           backup_unit="The previous release.", recovery="The previous release.",
           cadence_days=None, procedure=_vendored_procedure)
def read_vendored_frontend(ctx: Context) -> Reading:
    recorded = vendored.load(ctx.root)
    latest = _latest(ctx, "vendored_frontend", needs_network=True)
    if recorded is None:
        unknown = Fact.unknown("VENDORED.json", "no VENDORED.json: run scripts/components.py vendored")
        return _reading(ctx, "vendored_frontend", unknown, unknown, latest,
                        columns=("File", "Kind", "Version", "SHA-256 recorded", "State"), rows=[])
    rows = vendored.verify(ctx.root, recorded)
    sha = vendored.sha256_file(ctx.root / vendored.FILE)
    built = _fact(f"{len(rows)} files as recorded", f"VENDORED.json (sha256 {sha[:12]})", ctx)
    differ = [r for r in rows if r["state"] == "differs"]
    absent = [r for r in rows if r["state"] == "not in this tree"]
    if differ:
        running = _fact(f"{len(differ)} of {len(rows)} files differ from VENDORED.json",
                        "sha256 of each file in this tree", ctx)
    else:
        running = _fact(built.value, "sha256 of each file in this tree", ctx)
    note = f"{len(absent)} recorded files are not in this tree (the image ships core/ only)" if absent else ""
    entry = _manifest_entry(ctx, "vendored_frontend")
    return _reading(ctx, "vendored_frontend", running, built, latest,
                    columns=("File", "Kind", "Version", "SHA-256 recorded", "State"),
                    rows=[(r["path"], r["kind"], r["version"], r["sha256"][:12], r["state"]) for r in rows],
                    note=note, newer=lambda _l, _b: _upstream_newer(entry))


def _infra_procedure(ctx: Context) -> tuple[Step, ...]:
    tf = "terraform -chdir=deploy/aws"
    return _steps("guided", [
        (f"{tf} init -backend-config=backend.hcl && {tf} plan -out=tfplan && {tf} show -no-color tfplan > tfplan.txt",
         "The plan is attached to the checklist; the screen lists every resource it would change. Attest that "
         "the plan shown is the plan reviewed.", AWS_RUNBOOK),
        (f"{tf} state pull > state-before-$(date +%Y%m%d).json && python3 -c \"import json,sys; "
         "print(json.load(open(sys.argv[1]))['serial'])\" state-before-$(date +%Y%m%d).json",
         "Attest: record the state serial the plan was made against (the state bucket keeps every version).",
         AWS_RUNBOOK),
        (f"{tf} apply tfplan",
         "Attest: neither mode applies Terraform from the platform; the admin applies the reviewed plan from "
         "the workstation.", AWS_RUNBOOK),
        (f"{tf} state pull | python3 -c \"import json,sys; print(json.load(sys.stdin)['serial'])\" && "
         "python -m core.healthcheck",
         "The healthcheck is green by machine; attest that the state serial advanced by one and the RDS engine "
         "and bucket settings read what the plan said.", AWS_RUNBOOK),
        (f"git checkout <previous commit> -- deploy/aws && {tf} plan -out=tfplan-back && {tf} apply tfplan-back",
         "The healthcheck is green by machine; attest the serial and the settings before recovered is shown.",
         AWS_RUNBOOK),
    ])


@component(key="infra_pins", group="B", name="Infra pins", kind="terraform pins", mode="guided",
           backup_unit="Terraform state, versioned in the state bucket (S3-native locking).",
           recovery="Guided: apply the previous plan.",
           cadence_days=CADENCE_DAYS["advisories"], procedure=_infra_procedure)
def read_infra_pins(ctx: Context) -> Reading:
    text = _read(ctx.root, "deploy/aws/versions.tf")
    latest = _latest(ctx, "infra_pins", needs_network=True)
    if text is None:
        unknown = Fact.unknown("deploy/aws/versions.tf", "no deploy/aws/versions.tf under the root")
        return _reading(ctx, "infra_pins", unknown, unknown, latest, columns=("Pin", "Value", "Source"), rows=[])
    req, providers = _tf_pins(text)
    base = _dockerfile_from(_read(ctx.root, "Dockerfile")) or "unknown"
    declared = f"terraform {req or 'unpinned'}; " + "; ".join(f"{n} {v or 'unpinned'}" for n, _, v in providers) + \
               f"; base {base}"
    built = _fact(declared, "deploy/aws/versions.tf, Dockerfile FROM", ctx)
    rows = [("terraform required_version", req or "unpinned", "deploy/aws/versions.tf")]
    for name, src, ver in providers:
        rows.append((f"provider {name} ({src})", ver or "unpinned", "deploy/aws/versions.tf"))
    rows.append(("base image", f"{base} (floating tag)" if base.endswith("-slim") and base.count(":") == 1 else base,
                 "Dockerfile FROM"))
    lock = _read(ctx.root, "deploy/aws/.terraform.lock.hcl")
    if lock is None:
        running = Fact.unknown("deploy/aws/.terraform.lock.hcl",
                               "no .terraform.lock.hcl: terraform init has not selected providers for this checkout")
    else:
        selected = _tf_lock(lock)
        bad, undecided = [], []
        for name, _, constraint in providers:
            sel = selected.get(name)
            if sel is None:
                bad.append(f"{name} not selected")
                rows.append((f"selected {name}", "not in the lock file", "deploy/aws/.terraform.lock.hcl"))
                continue
            ok = _satisfies(sel, constraint) if constraint else None
            state = "satisfies" if ok else ("outside" if ok is False else "constraint not decided")
            if ok is False:
                bad.append(f"{name} {sel} outside {constraint}")
            elif ok is None:
                undecided.append(name)
            rows.append((f"selected {name}", f"{sel} ({state} {constraint})", "deploy/aws/.terraform.lock.hcl"))
        if bad:
            running = _fact("; ".join(bad), "deploy/aws/.terraform.lock.hcl against versions.tf", ctx)
        else:
            running = Fact(value=declared, checked_at=ctx.checked_at,
                           source="deploy/aws/.terraform.lock.hcl: the selected providers satisfy the declared "
                                  "constraints (the applied state is read by terraform plan on the workstation)")
    rds = _read(ctx.root, "deploy/aws/rds.tf")
    if rds:
        engv = re.search(r'^\s*engine_version\s*=\s*"([^"]+)"', rds, re.M)
        rows.append(("RDS engine_version", engv.group(1) if engv else "unknown", "deploy/aws/rds.tf"))
    entry = _manifest_entry(ctx, "infra_pins")
    return _reading(ctx, "infra_pins", running, built, latest, columns=("Pin", "Value", "Source"), rows=rows,
                    newer=lambda _l, _b: _upstream_newer(entry))


# ---------------------------------------------------------------------------
# C. Reference data on someone else's cadence
# ---------------------------------------------------------------------------

def _emr_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("guided", [
        ("scripts/components.py check   # slice 4: vendor page hashes against the ones the profiles cite",
         "Attest: the changed vendor page, the profile fields it may affect and the vendor's own documentation "
         "URL are named in the plan.", EMR_RUNBOOK),
        ("git tag -f pre-profile-change && git log -1 --format=%H -- core/fhir/emr_profiles.py",
         "Attest: the previous release stays kept; a profile change is a code change and ships in a release.",
         EMR_RUNBOOK),
        ("$EDITOR core/fhir/emr_profiles.py docs/EMR_CONNECTORS.md   # re-read the "
         "vendor's page, update the profile and its citation, record the date checked",
         "Attest: the profile and its documentation were changed together, citing the vendor's page.", EMR_RUNBOOK),
        (".venv/bin/python -m pytest -q tests/test_emulator_integration.py",
         "The emulator for that vendor still answers the profile's grant; the screen reads the test exit "
         "code as attested by the admin.", EMR_RUNBOOK),
        ("git checkout pre-profile-change -- core/fhir/emr_profiles.py",
         "Attest: the previous profile is back and the coverage test passes again.", EMR_RUNBOOK),
    ])


@component(key="emr_profiles", group="C", name="EMR vendor profiles", kind="vendor profiles", mode="guided",
           backup_unit="The previous release.", recovery="The previous release.",
           cadence_days=CADENCE_DAYS["vendor_docs"], procedure=_emr_procedure)
def read_emr_profiles(ctx: Context) -> Reading:
    """The vendor profile table, and how long since anyone checked it.

    THE ONLY HONEST QUESTION THIS ROW CAN ASK. Every field in
    core/fhir/emr_profiles.py is a claim about somebody else's API,
    transcribed from a page that vendor can change without telling anyone.
    Nothing here can verify those claims are TRUE - so this asks the
    question it can answer: when did a human last open the vendor's own
    documentation and confirm them, and against which page.

    BOTH FIELDS OR NEITHER. A doc_checked date with no doc_source is
    somebody's memory of having checked; the reverse is a bookmark. A
    profile carrying only one of the two is reported as an ERROR rather
    than counted as dated, because a half-recorded check reads as a check.

    A DATE OLDER THAN THE CADENCE IS NOT GREEN. It was, once: the reading
    counted dated profiles and stopped there, so a check from three years
    ago and one from this morning looked identical.
    """
    from core.fhir.emr_profiles import PROFILES

    n = len(PROFILES)
    built = _fact(f"{n} vendor profiles", "core/fhir/emr_profiles.py PROFILES", ctx)
    running = _same(built, "PROFILES as imported by this process")

    cadence = CADENCE_DAYS["vendor_docs"]
    today = now().date()
    rows: list[tuple] = []
    dated: list[str] = []
    stale: list[str] = []
    broken: list[str] = []

    for key, p in sorted(PROFILES.items()):
        checked, source = p.doc_checked.strip(), p.doc_source.strip()
        if bool(checked) != bool(source):
            broken.append(key)
            when = "INCOMPLETE: " + ("date without a source" if checked
                                     else "source without a date")
        elif not checked:
            when = "not recorded"
        else:
            try:
                age = (today - date.fromisoformat(checked)).days
            except ValueError:
                broken.append(key)
                age, when = None, f"INCOMPLETE: {checked!r} is not an ISO date"
            if age is not None:
                dated.append(key)
                when = checked if age <= cadence else f"{checked} ({age}d - due)"
                if age > cadence:
                    stale.append(key)
        rows.append((p.name, p.auth_flow, p.assertion_algorithm,
                     "yes" if p.supports_bulk_export else "no",
                     ", ".join(p.writable_resources) or "none", when, source or "-"))

    if broken:
        latest = Fact.unknown(
            "EMRProfile.doc_checked",
            f"{len(broken)} profile(s) record half a check ({', '.join(sorted(broken))}); "
            "a date and the source that was read are only meaningful together",
        )
    elif not dated:
        latest = Fact.unknown(
            "EMRProfile.doc_checked",
            "the profiles record no doc_checked date; a vendor page re-check has never "
            f"been recorded (cadence {cadence} days)",
        )
    elif len(dated) < n:
        latest = Fact.unknown(
            "EMRProfile.doc_checked",
            f"{n - len(dated)} of {n} profiles record no doc_checked date "
            f"({', '.join(k for k in sorted(PROFILES) if k not in dated)})",
        )
    elif stale:
        latest = Fact.unknown(
            "EMRProfile.doc_checked",
            f"{len(stale)} of {n} profiles were last checked more than {cadence} days "
            f"ago ({', '.join(sorted(stale))})",
        )
    else:
        latest = _fact(f"all {n} profiles checked within {cadence} days",
                       "EMRProfile.doc_checked", ctx)

    changed = build.git(ctx.root, "log", "-1", "--format=%cs", "--", "core/fhir/emr_profiles.py")
    rows.append(("profiles last changed", changed or "unknown: no git checkout",
                 "git log -1 --format=%cs -- core/fhir/emr_profiles.py"))
    note = ""
    if len(dated) < n:
        note = (f"{len(dated)} of {n} vendor profiles record which page was read and when; "
                "the rest are behind vendor developer-portal logins")
    return _reading(ctx, "emr_profiles", running, built, latest,
                    columns=("Vendor", "Auth flow", "Assertion", "Bulk export", "Writes",
                             "Doc checked", "Source"),
                    rows=rows, note=note)


def _terminology_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("direct", [
        ("", "The target release per source and the credential or file it loads from, with the counts expected "
             "after the load; the admin confirms by typing 'terminology <release>'.", "docs/SPEC.md §7.4"),
        ("psql \"$PHI_AI_DSN\" -c 'ALTER SCHEMA vocab RENAME TO vocab_previous'",
         "The current vocabulary schema is kept as vocab_previous (information_schema.schemata shows it); "
         "nothing is dropped before the swap.", "docs/SPEC.md §7.4"),
        ("python -m core.terminology.loader --source <name> --release <path or credential> --schema vocab_staging   "
         "# slice 3: staging-and-swap is not yet implemented; run guided",
         "The updater loads the release into a staging schema and swaps it in; until slice 3 the admin runs the "
         "load and attests.", "docs/SPEC.md §7.4"),
        ("psql \"$PHI_AI_DSN\" -c 'SELECT vocabulary_id, COUNT(*) FROM vocab.concept GROUP BY 1 ORDER BY 1'",
         "The vocabulary counts match the release, the expansion report lists every licensed expansion enabled, "
         "and the healthcheck is green.", "docs/SPEC.md §7.4"),
        ("psql \"$PHI_AI_DSN\" -c 'ALTER SCHEMA vocab RENAME TO vocab_failed; ALTER SCHEMA vocab_previous RENAME TO vocab'",
         "The previous schema is back (information_schema.schemata) and the counts match again.", "docs/SPEC.md §7.4"),
    ])


@component(key="terminology", group="C", name="Terminology releases", kind="terminology sources", mode="direct",
           backup_unit="The previous vocabulary schema, kept on swap.", recovery="Swap the previous schema back.",
           cadence_days=None, procedure=_terminology_procedure)
def read_terminology(ctx: Context) -> Reading:
    from core.terminology.loader import SOURCES

    built = _fact(f"{len(SOURCES)} sources declared", "core/terminology/loader.py SOURCES", ctx)
    latest = _latest(ctx, "terminology", needs_network=True)
    rows = [(s.name, s.license_class.value, s.expansion or "none", "not recorded", "unknown")
            for _, s in sorted(SOURCES.items())]

    def counts(conn):
        cur = conn.cursor()
        try:
            cur.execute("SELECT vocabulary_id, COUNT(*) FROM vocab.concept GROUP BY vocabulary_id ORDER BY vocabulary_id")
            return [(str(r[0]), int(r[1])) for r in cur.fetchall()]
        finally:
            cur.close()

    loaded, why = _with_conn(ctx, counts)
    if loaded is None:
        running = Fact.unknown("vocab.concept", f"loaded release ids are not recorded (slice 3); {why}")
    else:
        total = sum(n for _, n in loaded)
        running = Fact.unknown("vocab.concept",
                               f"loaded release ids are not recorded (slice 3 records them at the swap); "
                               f"vocab.concept holds {total} concepts in {len(loaded)} vocabularies")
        for vocab_id, n in loaded:
            rows.append((f"vocab.concept {vocab_id}", "", "", "not recorded", str(n)))
    entry = _manifest_entry(ctx, "terminology")
    return _reading(ctx, "terminology", running, built, latest,
                    columns=("Source", "Licence class", "Expansion", "Loaded release", "Concepts loaded"), rows=rows,
                    note="no table records which release of each source is loaded today",
                    newer=lambda _l, _b: _upstream_newer(entry))


def _model_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("direct", [
        ("", "The provider deprecation lists in the manifest are compared with every registered model_id; the "
             "plan names the IDs to retire and the slots they serve; the admin confirms by typing "
             "'model_catalogue <model_id>'.", MODEL_RUNBOOK),
        ("psql \"$PHI_AI_DSN\" -c \"SELECT id, name, status FROM platform_models WHERE model_id = '<model_id>'\"",
         "The engine records each affected registry row as it stands (id, name, status).", MODEL_RUNBOOK),
        ("psql \"$PHI_AI_DSN\" -c \"UPDATE platform_models SET status = 'retired' WHERE model_id = '<model_id>'\"",
         "The engine marks the retired ID retired in the registry, audited; choosing the replacement stays the "
         "Control panel's register, enable, activate flow.", MODEL_RUNBOOK),
        ("psql \"$PHI_AI_DSN\" -c \"SELECT id, status FROM platform_models WHERE model_id = '<model_id>'\" && "
         "python -m core.healthcheck",
         "Every row carrying the ID reads retired, every slot still resolves to an enabled model or shows the "
         "degraded banner, and the healthcheck is green.", MODEL_RUNBOOK),
        ("psql \"$PHI_AI_DSN\" -c \"UPDATE platform_models SET status = '<previous status>' WHERE id = <id>\"",
         "The row's previous status is back and the verification runs again.", MODEL_RUNBOOK),
    ])


def _default_model(root: Path) -> Optional[str]:
    text = _read(root, "core/assistant/config.py")
    m = re.search(r'^DEFAULT_MODEL\s*=\s*"([^"]+)"', text or "", re.M)
    return m.group(1) if m else None


@component(key="model_catalogue", group="C", name="Model catalogue", kind="model registry", mode="direct",
           backup_unit="The registry row's previous state.", recovery="Restore the row.",
           cadence_days=CADENCE_DAYS["advisories"], procedure=_model_procedure)
def read_model_catalogue(ctx: Context) -> Reading:
    from core.web.platform_state import PlatformState

    default = _default_model(ctx.root)
    shipped = sorted({m["model_id"] for m in PlatformState().list_models() if m.get("builtin") and m.get("model_id")}
                     | ({default} if default else set()))
    built = _fact("shipped ids enabled: " + ", ".join(shipped),
                  "core/web/platform_state.py builtin models + core/assistant/config.py DEFAULT_MODEL", ctx)
    latest = _latest(ctx, "model_catalogue", needs_network=True)
    deprecated = set((_manifest_entry(ctx, "model_catalogue").get("deprecated") or []))
    rows = []
    if ctx.platform_state is None:
        running = Fact.unknown("platform_state models", "no platform state in this process")
    else:
        models = ctx.platform_state.list_models()
        by_id: dict[str, list[dict]] = {}
        for m in models:
            by_id.setdefault(m.get("model_id", ""), []).append(m)
            rows.append((m["name"], m["kind"], m["provider"], m["model_id"] or "(built-in)", m["status"],
                         "retired upstream" if m["model_id"] in deprecated else
                         ("not checked" if not _manifest_entry(ctx, "model_catalogue") or
                          manifest.is_offline(_manifest_entry(ctx, "model_catalogue")) else "no")))
        missing = [i for i in shipped if not any(m["status"] == "enabled" for m in by_id.get(i, []))]
        if missing:
            running = _fact(f"{len(missing)} shipped ids not enabled: " + ", ".join(missing), "platform_state models", ctx)
        else:
            running = _fact(built.value, "platform_state models", ctx)
    rows.sort(key=lambda r: r[0].lower())
    entry = _manifest_entry(ctx, "model_catalogue")
    return _reading(ctx, "model_catalogue", running, built, latest,
                    columns=("Model", "Kind", "Provider", "Model ID", "Status", "Retired upstream"), rows=rows,
                    note=f"platform default {default}" if default else "core/assistant/config.py names no DEFAULT_MODEL",
                    newer=lambda _l, _b: _upstream_newer(entry))


def _corpus_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The generator, its version and the seed are read from the provenance manifest and shown; nothing "
             "to confirm.", "docs/TESTDATA.md"),
        ("", "Nothing is backed up; the manifest is the backup, and it is committed.", "docs/TESTDATA.md"),
        ("", "Nothing is applied; a new corpus is a new run of scripts/generate_corpus.py with a new recorded seed.",
         "docs/TESTDATA.md"),
        ("", "The bundle count and the resources marked match tests/fixtures/layer2.MANIFEST.json.", "docs/TESTDATA.md"),
        ("", "Nothing is recovered; regenerate from the recorded seed.", "docs/TESTDATA.md"),
    ])


@component(key="synthetic_corpus", group="C", name="Synthetic corpus", kind="synthetic corpus", mode="record",
           backup_unit="n/a: the corpus is reproducible from generator, version and seed.",
           recovery="n/a: regenerate from the recorded seed.",
           cadence_days=None, procedure=_corpus_procedure)
def read_synthetic_corpus(ctx: Context) -> Reading:
    man_rel = "tests/fixtures/layer2.MANIFEST.json"
    man = _json_file(ctx.root, man_rel)
    script = _read(ctx.root, "scripts/generate_corpus.py")
    ver = re.search(r'^SYNTHEA_VERSION\s*=\s*"([^"]+)"', script or "", re.M)
    seed = re.search(r'^SEED\s*=\s*"([^"]+)"', script or "", re.M)
    if script is None:
        running = Fact.unknown("scripts/generate_corpus.py", "no scripts/generate_corpus.py under the root")
    elif not (ver and seed):
        running = Fact.unknown("scripts/generate_corpus.py", "the generator declares no SYNTHEA_VERSION / SEED literal")
    else:
        running = _fact(f"synthea {ver.group(1)} seed {seed.group(1)}", "scripts/generate_corpus.py SYNTHEA_VERSION, SEED", ctx)
    if man is None:
        built = Fact.unknown(man_rel, f"no {man_rel}")
    else:
        built = _fact(f"{man.get('generator', 'unknown')} {man.get('generator_version', 'unknown')} seed {man.get('seed', 'unknown')}",
                      man_rel, ctx)
    latest = _same(built, f"{man_rel} is the record; the corpus is reproducible from it") if built.known else built
    rows = []
    if man:
        for field in ("generator", "generator_version", "seed", "bundles", "resources_marked", "jar_sha256", "generated_at"):
            rows.append((field, str(man.get(field, "absent")), man_rel))
    rows.append(("generator script version", ver.group(1) if ver else "absent", "scripts/generate_corpus.py"))
    rows.append(("generator script seed", seed.group(1) if seed else "absent", "scripts/generate_corpus.py"))
    return _reading(ctx, "synthetic_corpus", running, built, latest, columns=("Fact", "Value", "Source"), rows=rows)


# ---------------------------------------------------------------------------
# D. State, schema, secrets
# ---------------------------------------------------------------------------

def _ledger_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("guided", [
        ("scripts/components.py ledger --dsn \"$PHI_AI_DSN\"   # every core/db/*.sql with its checksum and ledger row",
         "The SQL that will run, its checksum, whether it ships a down and the schemas it touches are listed; the "
         "admin confirms by typing 'migration_ledger <file>'.", INDEX_RUNBOOK),
        ("pg_dump --format=custom --schema public --file=restore-output/before-<file>.dump --dbname=\"$PHI_AI_DSN\"",
         "Attest the dump's path and sha256sum; the journal records them. No dump, no apply.", INDEX_RUNBOOK),
        ("psql \"$PHI_AI_DDL_DSN\" -v ON_ERROR_STOP=1 -f core/db/<file> && psql \"$PHI_AI_DDL_DSN\" -c \"INSERT INTO "
         "schema_migrations (name, checksum, applied_by, note) VALUES ('<file>', '<sha256>', '<you>', 'applied by hand')\"",
         "Attest: the DDL credential is used for this job only and never stored (the application roles hold no DDL).",
         INDEX_RUNBOOK),
        ("psql \"$PHI_AI_DSN\" -c \"SELECT name, checksum, applied_at FROM schema_migrations WHERE name = '<file>'\"",
         "information_schema shows the file's tables, the ledger row carries the file's checksum, and the "
         "healthcheck is green - verified by machine.", INDEX_RUNBOOK),
        ("psql \"$PHI_AI_DDL_DSN\" -f core/db/<file>.down.sql   # or: pg_restore --clean --dbname=\"$PHI_AI_DDL_DSN\" "
         "restore-output/before-<file>.dump",
         "The ledger row is gone or the dump is restored; information_schema no longer shows the objects; attest "
         "which path was taken.", INDEX_RUNBOOK),
    ])


def _other_cloud(name: str, provider: str) -> bool:
    m = re.search(r"_(aws|azure|gcp)\.sql$", name)
    return bool(m and provider and m.group(1) != provider)


@component(key="migration_ledger", group="D", name="Migration ledger", kind="schema files", mode="guided",
           backup_unit="pg_dump of the affected schemas, taken by the updater before the up.",
           recovery="Run the down; restore the dump if the down cannot.",
           cadence_days=None, procedure=_ledger_procedure)
def read_migration_ledger(ctx: Context) -> Reading:
    if not (ctx.root / "core" / "db").is_dir():
        unknown = Fact.unknown("core/db/*.sql", "no core/db directory under the root")
        return _reading(ctx, "migration_ledger", unknown, unknown, unknown,
                        columns=("File", "Tables", "SHA-256", "Ledger", "Applied at", "By"), rows=[])
    provider = (env_var("CLOUD_PROVIDER", "") or "").lower()
    rows_status, why = _with_conn(ctx, lambda conn: ledger.status(ctx.root, conn))
    if rows_status is None:
        rows_status = ledger.status(ctx.root, None)
    expected = [r for r in rows_status if not _other_cloud(r["name"], provider)]
    applied_tables: set[tuple[str, str]] = set()
    for r in rows_status:
        if r["ledger"] == "applied":
            applied_tables.update(r["tables"])
    n = len(expected)
    built = _fact(f"{n} of {n} schema files applied with matching checksums",
                  f"core/db/*.sql (glob, {len(rows_status)} files" + (f"; {len(rows_status) - n} for other clouds" if n != len(rows_status) else "") + ")",
                  ctx)
    latest = _same(built, "the tree is the latest known: schema files ship in a release")
    rows = []
    applied = 0
    for r in rows_status:
        state = r["ledger"]
        if state == "not in the ledger" and r["tables"] and any(t in applied_tables for t in r["tables"]):
            alt = [o["name"] for o in rows_status if o["ledger"] == "applied" and set(o["tables"]) & set(r["tables"])]
            state = f"alternative of {', '.join(alt)}"
        if _other_cloud(r["name"], provider):
            state = f"for another cloud ({state})"
        if state == "applied" or state.startswith("alternative of"):
            if r in expected:
                applied += 1
        rows.append((r["name"], ", ".join(f"{s}.{t}" if s != "public" else t for s, t in r["tables"]) or "none",
                     r["sha256"][:12], state, r["applied_at"], r["applied_by"]))
    if why:
        running = Fact.unknown("schema_migrations", why)
    elif applied == n:
        running = _fact(built.value, "schema_migrations against core/db/*.sql", ctx)
    else:
        running = _fact(f"{applied} of {n} schema files applied with matching checksums",
                        "schema_migrations against core/db/*.sql", ctx)
    return _reading(ctx, "migration_ledger", running, built, latest,
                    columns=("File", "Tables", "SHA-256", "Ledger", "Applied at", "By"), rows=rows,
                    note="" if not why else "the ledger is read from the index; " + why)


def _keys_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("guided", [
        ("ls -l --time-style=long-iso epic_*.pem config/*.pem config/*jwks*.json 2>/dev/null",
         "Which key is due, the JWKS it is published in and the vendor that must confirm it are named; the admin "
         "confirms by typing 'key_ages <key file>'.", EMR_RUNBOOK),
        ("mkdir -p keys-previous && cp -p epic_private_key.pem epic_public_key.pem keys-previous/",
         "Attest: the previous key pair is retained and the JWKS it is in is recorded; nothing is deleted before "
         "the vendor confirms.", EMR_RUNBOOK),
        ("scripts/generate_epic_keypair.sh . && $EDITOR deploy/aws/epic_jwks_nonprod.json   # publish the JWKS with "
         "both kids, then register it with the vendor",
         "Attest: the new pair is generated and the JWKS carries both kids.", EMR_RUNBOOK),
        ("curl -fsS <JWKS URL> | python3 -c \"import json,sys; print([k['kid'] for k in json.load(sys.stdin)['keys']])\" "
         "&& python -m core.healthcheck",
         "The screen reads the key file ages by machine (a new mtime within cadence); attest that the JWKS URL "
         "serves the new kid and a token request with the new key succeeds against the vendor's sandbox.", EMR_RUNBOOK),
        ("cp -p keys-previous/epic_private_key.pem epic_private_key.pem && cp -p keys-previous/epic_public_key.pem "
         "epic_public_key.pem   # re-publish the previous JWKS",
         "Attest: the previous JWKS is served and a token request with the previous key succeeds.", EMR_RUNBOOK),
    ])


@component(key="key_ages", group="D", name="Key and credential ages", kind="key ages", mode="guided",
           backup_unit="The previous key, retained until the vendor confirms the new one.",
           recovery="Re-publish the previous JWKS.",
           cadence_days=CADENCE_DAYS["keys"], procedure=_keys_procedure)
def read_key_ages(ctx: Context) -> Reading:
    root = ctx.root
    files = sorted(root.glob("epic_*.pem")) + sorted((root / "config").glob("*.pem")) + \
        sorted((root / "config").glob("*jwks*.json"))
    files = [f for f in files if f.is_file()]
    source = "file mtimes of epic_*.pem, config/*.pem, config/*jwks*.json (ages only; contents never read)"
    if not files:
        unknown = Fact.unknown(source, "no key files found under the root")
        return _reading(ctx, "key_ages", unknown, unknown, unknown,
                        columns=("File", "Age (days)", "Modified", "Rotation due", "State"), rows=[])
    at = ctx.checked_at
    cadence = timedelta(days=CADENCE_DAYS["keys"])
    rows, ages, dues = [], [], []
    for f in files:
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        age = (at - mtime).days
        due = (mtime + cadence).date()
        ages.append(age)
        dues.append(due)
        rows.append((f.relative_to(root).as_posix(), str(age), mtime.date().isoformat(), due.isoformat(),
                     "within cadence" if due >= at.date() else "overdue"))
    running = _fact(f"{len(files)} key files; oldest {max(ages)} days", source, ctx)
    built = _same(running, "the same files: a key is not built; its age is the only fact")
    due = min(dues)
    latest = _fact(f"rotation due {due.isoformat()}", f"mtime + CADENCE_DAYS['keys'] ({CADENCE_DAYS['keys']} days)", ctx)
    today = at.date()
    return _reading(ctx, "key_ages", running, built, latest,
                    columns=("File", "Age (days)", "Modified", "Rotation due", "State"), rows=rows,
                    newer=lambda _l, _b: due < today,
                    note="" if due >= today else f"rotation was due {due.isoformat()}")


def _config_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("direct", [
        ("", "Each config/*.yaml is compared key by key with its shipped example; the plan lists the missing keys "
             "and the defaults that would fill them; the admin confirms by typing 'operator_config <file>'.",
         RETENTION_RUNBOOK),
        ("cp -p config/<file>.yaml config/<file>.yaml.previous && sha256sum config/<file>.yaml.previous",
         "The engine copies the current file to <file>.previous and records its sha256.", RETENTION_RUNBOOK),
        ("diff <(python3 -c \"import yaml,sys; print(sorted(yaml.safe_load(open(sys.argv[1]))))\" config/<file>.example.yaml) "
         "<(python3 -c \"import yaml,sys; print(sorted(yaml.safe_load(open(sys.argv[1]))))\" config/<file>.yaml)   # then "
         "append the missing keys from the example",
         "The engine writes the missing top-level keys with the example's defaults and keeps every existing line.",
         RETENTION_RUNBOOK),
        ("python3 -c \"import yaml,sys; yaml.safe_load(open(sys.argv[1]))\" config/<file>.yaml && python -m core.healthcheck",
         "The file parses, every key the example expects is present, and the healthcheck is green - by machine.",
         RETENTION_RUNBOOK),
        ("cp -p config/<file>.yaml.previous config/<file>.yaml",
         "The previous file is back (its sha256 matches the recorded one) and the verification runs again.",
         RETENTION_RUNBOOK),
    ])


@component(key="operator_config", group="D", name="Operator config files", kind="config files", mode="direct",
           backup_unit="The previous file, kept beside the dump.", recovery="Restore it.",
           cadence_days=None, procedure=_config_procedure)
def read_operator_config(ctx: Context) -> Reading:
    from core.components.journal import missing_config_keys

    import yaml

    examples = sorted((ctx.root / "config").glob("*.example.yaml"))
    source = "config/*.yaml compared key by key with config/*.example.yaml"
    if not examples:
        unknown = Fact.unknown("config/*.example.yaml", "no shipped examples under config/")
        return _reading(ctx, "operator_config", unknown, unknown, unknown,
                        columns=("Example", "Operator file", "Keys expected", "Missing"), rows=[])
    rows, present, missing_total, expected_total = [], 0, 0, 0
    for ex in examples:
        name = ex.name.replace(".example.yaml", ".yaml")
        target = ctx.root / "config" / name
        try:
            ex_data = yaml.safe_load(ex.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            rows.append((ex.name, name, "example does not parse", "unknown"))
            continue
        keys = sorted(ex_data.keys()) if isinstance(ex_data, dict) else []
        expected_total += len(keys)
        if not target.is_file():
            rows.append((ex.name, f"{name} absent (feature not configured)", ", ".join(keys), "n/a"))
            continue
        present += 1
        try:
            actual = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            rows.append((ex.name, name, ", ".join(keys), f"does not parse: {type(exc).__name__}"))
            missing_total += len(keys)
            continue
        missing = missing_config_keys(ex_data, actual)
        missing_total += len(missing)
        rows.append((ex.name, name, ", ".join(keys), ", ".join(missing) or "none"))
    running = _fact(f"{present} operator files present of {len(examples)} examples; {missing_total} keys missing", source, ctx)
    built = _same(running, source)
    latest = _fact(f"{len(examples)} examples in the tree, {expected_total} top-level keys expected",
                   "config/*.example.yaml (glob)", ctx)
    return _reading(ctx, "operator_config", running, built, latest,
                    columns=("Example", "Operator file", "Keys expected", "Missing"), rows=rows,
                    newer=lambda _l, _b: missing_total > 0,
                    note="" if not missing_total else f"{missing_total} keys the release expects are missing")


# ---------------------------------------------------------------------------
# E. Verification and recovery
# ---------------------------------------------------------------------------

def _gates_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The gates the pre-push script runs are listed with the commit they last passed on; nothing to confirm.",
         "scripts/pre_push_gates.sh"),
        ("", "Nothing is backed up; the evidence is .gates/last_green.json when the script writes it.",
         "scripts/pre_push_gates.sh"),
        ("", "Nothing is applied; the gates run on the workstation before every push (scripts/pre_push_gates.sh).",
         "scripts/pre_push_gates.sh"),
        ("", "The running build's commit appears in .gates/last_green.json with every gate green - by machine.",
         "scripts/pre_push_gates.sh"),
        ("", "Nothing is recovered; a release without green gates is not built.", "scripts/pre_push_gates.sh"),
    ])


@component(key="last_green_gates", group="E", name="Last green gates", kind="gate record", mode="record",
           backup_unit="n/a: a record of the gates, not a store.",
           recovery="n/a: a build whose commit never had green gates is not released.",
           cadence_days=None, procedure=_gates_procedure)
def read_last_green_gates(ctx: Context) -> Reading:
    record_rel = ".gates/last_green.json"
    record = _json_file(ctx.root, record_rel)
    facts = build.git_facts(ctx.root)
    stamp = ctx.build if ctx.build is not None else build.read_stamp(ctx.root)
    if facts["commit"] != build.UNKNOWN:
        running = _fact(f"commit {build.short(facts['commit'])}", "git rev-parse HEAD", ctx)
    elif stamp and stamp.get("commit"):
        running = _fact(f"commit {build.short(stamp['commit'])}", "BUILD.json commit", ctx)
    else:
        running = Fact.unknown("git / BUILD.json", "the running commit cannot be named: no checkout and no stamp")
    if record and record.get("commit"):
        built = _fact(f"commit {build.short(str(record['commit']))}", record_rel, ctx)
    elif record:
        built = Fact.unknown(record_rel, f"{record_rel} names no commit")
    else:
        built = Fact.unknown(record_rel, f"no {record_rel}: scripts/pre_push_gates.sh does not write one today")
    latest = _same(built, f"{record_rel} is the record of the last green run") if built.known else built
    rows = []
    script = _read(ctx.root, "scripts/pre_push_gates.sh")
    for name in re.findall(r'^echo\s+"(pre-push gate [^"]+)"', script or "", re.M):
        rows.append((name, "declared by the script", "scripts/pre_push_gates.sh"))
    if not script:
        rows.append(("gates", "unknown: no scripts/pre_push_gates.sh", "scripts/pre_push_gates.sh"))
    if record:
        for field in ("commit", "at", "gates", "python"):
            if field in record:
                value = record[field]
                rows.append((f"record {field}", ", ".join(map(str, value)) if isinstance(value, list) else str(value),
                             record_rel))
    return _reading(ctx, "last_green_gates", running, built, latest, columns=("Gate", "Last result", "Source"), rows=rows)


def _backups_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The last backup per store, where it is and its checksum are read from the journal; nothing to confirm.",
         VERIFY_RUNBOOK),
        ("", "The backups are the record; nothing further is copied here.", VERIFY_RUNBOOK),
        ("", "Nothing is applied; a rehearsal restores one store to a scratch schema and is recorded "
             "(Journal.rehearsed).", VERIFY_RUNBOOK),
        ("", f"Each store's last backup verifies against its checksum and its rehearsal is within "
             f"{CADENCE_DAYS['rehearsal']} days - by machine from the journal.", VERIFY_RUNBOOK),
        ("", "Nothing is recovered here; each component row names its own recovery step.", VERIFY_RUNBOOK),
    ])


@component(key="backups", group="E", name="Backups", kind="backup record", mode="record",
           backup_unit="Each store, per its row: the updater's dumps under system/backups/, RDS automated backups, S3 versioning.",
           recovery="Restore is one command the updater can run; a rehearsal is recorded per store on its cadence.",
           cadence_days=CADENCE_DAYS["rehearsal"], procedure=_backups_procedure)
def read_backups(ctx: Context) -> Reading:
    j = _journal(ctx)
    latest_rows = j.latest_backups()
    columns = ("Store", "Where", "Last backup", "Checksum", "Verified", "Restore rehearsed", "Retention")
    if not latest_rows:
        unknown = Fact.unknown("journal platform_backups", "no backup recorded in the journal")
        return _reading(ctx, "backups", unknown, unknown, unknown, columns=columns, rows=[],
                        note="the updater records its dumps here (slice 2); an RDS snapshot can be attested")
    at = ctx.checked_at
    rows, verified, rehearsed, dues = [], 0, 0, []
    for store, r in sorted(latest_rows.items()):
        if r["verified"]:
            verified += 1
        reh = _parse_stamp(r.get("rehearsed_at", ""))
        if reh is not None:
            rehearsed += 1
            dues.append((reh + timedelta(days=CADENCE_DAYS["rehearsal"])).date())
        rows.append((store, r["location"] or "unrecorded", r["at"], r["checksum"] or "unrecorded",
                     "verified by the updater" if r["verified"] else "attested",
                     r.get("rehearsed_at") or "never", f"{KEEP['backups']} per store"))
    last = max(r["at"] for r in latest_rows.values())
    running = _fact(f"{len(latest_rows)} stores backed up ({verified} verified); last {last}; "
                    f"rehearsed {rehearsed} of {len(latest_rows)}", "journal platform_backups", ctx)
    built = _same(running, "the journal is the record; nothing is built")
    if rehearsed < len(latest_rows):
        latest = _fact(f"rehearsal never recorded for {len(latest_rows) - rehearsed} stores",
                       f"CADENCE_DAYS['rehearsal'] ({CADENCE_DAYS['rehearsal']} days)", ctx)
        overdue = True
    else:
        due = min(dues)
        latest = _fact(f"rehearsal due {due.isoformat()}", f"last rehearsal + {CADENCE_DAYS['rehearsal']} days", ctx)
        overdue = due < at.date()
    return _reading(ctx, "backups", running, built, latest, columns=columns, rows=rows,
                    newer=lambda _l, _b: overdue,
                    note="" if not overdue else "a restore rehearsal is due")


def _releases_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The digests kept and their stamps are listed; nothing to confirm.", RELEASE_CHECKLIST),
        ("", "The kept digests are the backups; nothing further is copied here.", RELEASE_CHECKLIST),
        ("", f"Nothing is applied; a digest is marked not kept only after a newer release exists "
             f"(KEEP images {KEEP['images']}).", RELEASE_CHECKLIST),
        ("", "Each kept digest is present in the registry and its stamp matches its release - the registry check "
             "is attested; the journal's record is by machine.", RELEASE_CHECKLIST),
        ("", "Nothing is recovered here; the Running image row rolls back to a kept digest.", RELEASE_CHECKLIST),
    ])


@component(key="releases_kept", group="E", name="Releases kept", kind="release record", mode="record",
           backup_unit="The kept image digests are the backup unit of every release-borne row.",
           recovery="compose up -d a kept digest; the verify sweep runs before recovered is shown.",
           cadence_days=None, procedure=_releases_procedure)
def read_releases_kept(ctx: Context) -> Reading:
    j = _journal(ctx)
    releases = j.releases()
    columns = ("Release", "Digest", "Recorded", "Commit", "Kept")
    if not releases:
        unknown = Fact.unknown("journal platform_releases",
                               "no release digest recorded: scripts/components.py build --image-digest, then "
                               "Journal.record_release")
        return _reading(ctx, "releases_kept", unknown, unknown, unknown, columns=columns, rows=[])
    kept = [r for r in releases if r["kept"]]
    running = _fact(f"{len(kept)} of {len(releases)} digests kept; newest {releases[0]['release']}",
                    "journal platform_releases", ctx)
    built = _same(running, "the journal is the record; nothing is built")
    latest = _fact(f"keep {KEEP['images']}", "registry KEEP['images']", ctx)
    rows = [(r["release"], r["digest"], r["recorded_at"], build.short((r.get("stamp") or {}).get("commit")),
             "kept" if r["kept"] else "not kept") for r in releases]
    return _reading(ctx, "releases_kept", running, built, latest, columns=columns, rows=rows,
                    newer=lambda _l, _b: len(kept) > KEEP["images"])


def _journal_procedure(ctx: Context) -> tuple[Step, ...]:
    return _steps("record", [
        ("", "The journal shows the last job, its mode and each step's outcome; nothing to confirm.", VERIFY_RUNBOOK),
        ("", "The journal is part of the operational state the updater dumps before any job.", VERIFY_RUNBOOK),
        ("", "Nothing is applied; every job writes its own steps here as they complete.", VERIFY_RUNBOOK),
        ("", "The last job's steps are all done and no job is open - by machine from platform_updates.", VERIFY_RUNBOOK),
        ("", "An open job on restart offers finish or roll back, and the screen shows which.", VERIFY_RUNBOOK),
    ])


@component(key="update_journal", group="E", name="Update journal", kind="journal", mode="record",
           backup_unit="The journal rows themselves live in the operational state, dumped before every job.",
           recovery="If the updater dies mid-job it reads the open job on restart and offers exactly one action: finish or roll back.",
           cadence_days=None, procedure=_journal_procedure)
def read_update_journal(ctx: Context) -> Reading:
    j = _journal(ctx)
    last = j.last_job()
    open_job = j.open_job()
    columns = ("Step", "Status", "Outcome", "Who", "When", "Attested", "Mode")
    if last is None:
        unknown = Fact.unknown("journal platform_updates", "no job recorded yet")
        return _reading(ctx, "update_journal", unknown, unknown, unknown, columns=columns, rows=[])
    done = sum(1 for s in last["steps"] if s["status"] == "done")
    running = _fact(f"last job {last['id']}: {last['component']} to {last['target']}, {last['mode']}, "
                    f"{last['status']}; {done} of {len(STEPS)} steps done", "journal platform_updates", ctx)
    built = _same(running, "the journal is the record; nothing is built")
    if open_job is None:
        latest = _fact("no job open; nothing waiting on anyone", "journal platform_updates", ctx)
    else:
        latest = _fact(f"job {open_job['id']} on {open_job['component']} open since {open_job['started_at']} "
                       f"by {open_job['started_by']}", "journal platform_updates", ctx)
    rows = [(s["step"], s["status"], s["outcome"] or "", s["actor"] or "", s["at"] or "",
             "attested" if s["attested"] else "", s["mode"] or "") for s in last["steps"]]
    return _reading(ctx, "update_journal", running, built, latest, columns=columns, rows=rows,
                    newer=lambda _l, _b: open_job is not None,
                    note="" if open_job is None else f"a job is open on {open_job['component']}")


__all__ = ["context"]
# Made by Ryan Gomez & Co. Inc.

