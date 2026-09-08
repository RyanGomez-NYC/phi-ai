# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The migration ledger: schema_migrations (core/db/components_schema.sql).

Every core/db/*.sql is listed by glob with its sha256 - never a typed
list. The ledger says which of them ran, with what checksum, when and by
whom. Today no ledger exists and every file that ever ran did so by hand,
so the one-time BACKFILL (proposal §14, decision 1) writes a row for each
file whose CREATE TABLE / CREATE SCHEMA / CREATE ROLE objects already
exist in the database, with the note 'backfill' - after a pg_dump of the
schemas it touches, or, when the operator has no pg_dump and says so, with
the attestation recorded in the note. Nothing else is written or altered.

The application roles hold no DDL by design (RUNBOOK_INDEX_MAINTENANCE.md):
applying a migration stays a guided step, or a direct one with a DDL
credential entered at the moment of use (slice 3). This module never runs
a migration file.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_FILE = "components_schema.sql"
DB_DIR = ("core", "db")
LEDGER_TABLE = "schema_migrations"

_CREATE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|SCHEMA|ROLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"\"?([A-Za-z_][\w]*)\"?(?:\s*\.\s*\"?([A-Za-z_][\w]*)\"?)?",
    re.IGNORECASE | re.MULTILINE,
)


class LedgerError(RuntimeError):
    """A backfill that cannot proceed safely: no pg_dump and no attestation,
    a failed dump, or a database that refused."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def objects_in(sql: str) -> dict:
    """The tables (as (schema, name)), schemas and roles a file CREATEs.
    Comments are stripped first so a commented-out statement is not an
    object. A table without a schema prefix is in public."""
    text = re.sub(r"--[^\n]*", "", sql)
    tables: list[tuple[str, str]] = []
    schemas: list[str] = []
    roles: list[str] = []
    for kind, first, second in _CREATE.findall(text):
        kind = kind.upper()
        if kind == "TABLE":
            entry = (first.lower(), second.lower()) if second else ("public", first.lower())
            if entry not in tables:
                tables.append(entry)
        elif kind == "SCHEMA":
            if first.lower() not in schemas:
                schemas.append(first.lower())
        elif kind == "ROLE":
            if first.lower() not in roles:
                roles.append(first.lower())
    return {"tables": tables, "schemas": schemas, "roles": roles}


def list_migrations(root: Path) -> list[dict]:
    """Every core/db/*.sql by glob, sorted by name, with its sha256 and the
    objects it creates."""
    db = Path(root).joinpath(*DB_DIR)
    out = []
    for path in sorted(db.glob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        row = {"name": path.name, "path": path, "sha256": sha256_file(path),
               "bytes": path.stat().st_size}
        row.update(objects_in(text))
        out.append(row)
    return out


def schema_sql(root: Path) -> str:
    return Path(root).joinpath(*DB_DIR, SCHEMA_FILE).read_text(encoding="utf-8")


def ensure_schema(conn: Any, root: Path) -> None:
    """Create the ledger and journal tables (safe to re-run)."""
    cur = conn.cursor()
    try:
        cur.execute(schema_sql(root))
    finally:
        cur.close()
    conn.commit()


def applied(conn: Any) -> list[dict]:
    """The ledger's rows, oldest first. An absent table reads as empty:
    the caller decides whether to create it."""
    cur = conn.cursor()
    try:
        try:
            cur.execute(f"SELECT name, checksum, applied_at, applied_by, note FROM {LEDGER_TABLE} "
                        "ORDER BY applied_at, name")
            rows = cur.fetchall()
        except Exception:  # noqa: BLE001 - no ledger yet is an answer, not a failure
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return []
    finally:
        cur.close()
    return [{"name": r[0], "checksum": r[1], "applied_at": r[2], "applied_by": r[3], "note": r[4]}
            for r in rows]


def existing_objects(conn: Any) -> dict:
    """What the database has: tables as (schema, name), schemas, roles."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')")
        tables = {(str(r[0]).lower(), str(r[1]).lower()) for r in cur.fetchall()}
        cur.execute("SELECT schema_name FROM information_schema.schemata")
        schemas = {str(r[0]).lower() for r in cur.fetchall()}
        cur.execute("SELECT rolname FROM pg_catalog.pg_roles")
        roles = {str(r[0]).lower() for r in cur.fetchall()}
    finally:
        cur.close()
    return {"tables": tables, "schemas": schemas, "roles": roles}


def presence(migration: dict, existing: dict) -> str:
    """'present' when every object the file creates exists, 'absent' when
    none does, 'partial' between, 'unverifiable' when the file creates no
    table, schema or role to look for."""
    wanted = [("tables", t) for t in migration["tables"]] + \
             [("schemas", s) for s in migration["schemas"]] + \
             [("roles", r) for r in migration["roles"]]
    if not wanted:
        return "unverifiable"
    found = sum(1 for kind, obj in wanted if obj in existing[kind])
    if found == len(wanted):
        return "present"
    return "absent" if found == 0 else "partial"


def touched_schemas(migrations: list[dict]) -> list[str]:
    """The schemas a backfill of these files concerns: the ledger's own
    (public) and every schema the files define objects in."""
    out = {"public"}
    for m in migrations:
        out.update(s for s, _ in m["tables"])
        out.update(m["schemas"])
    return sorted(out)


def record(conn: Any, name: str, checksum: str, applied_by: str, note: str) -> None:
    cur = conn.cursor()
    try:
        cur.execute(f"INSERT INTO {LEDGER_TABLE} (name, checksum, applied_by, note) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                    (name, checksum, applied_by, note))
    finally:
        cur.close()


def pg_dump(dsn: str, schemas: list[str], out: Path, *, runner: Callable = subprocess.run,
            which: Callable = shutil.which) -> dict:
    """pg_dump the named schemas to `out` (custom format). Raises
    LedgerError when pg_dump is not on PATH or fails."""
    exe = which("pg_dump")
    if not exe:
        raise LedgerError("pg_dump is not on PATH; install it or run with attest_no_dump "
                          "(--no-dump) to record that no dump was taken")
    argv = [exe, "--format=custom", f"--file={out}"]
    for s in schemas:
        argv += ["--schema", s]
    argv += [f"--dbname={dsn}"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run = runner(argv, capture_output=True, text=True, check=False)
    if getattr(run, "returncode", 1) != 0:
        raise LedgerError(f"pg_dump failed: {(getattr(run, 'stderr', '') or '').strip()[:400]}")
    return {"path": str(out), "schemas": list(schemas), "sha256": sha256_file(out) if Path(out).is_file() else "",
            "command": argv[:-1] + ["--dbname=<dsn>"]}


def backfill(conn: Any, applied_by: str, *, root: Path, dsn: Optional[str] = None,
             dump: bool = True, attest_no_dump: bool = False, dump_dir: Optional[Path] = None,
             runner: Callable = subprocess.run, which: Callable = shutil.which,
             at: Optional[datetime] = None) -> dict:
    """The one-time backfill. For each core/db/*.sql whose objects already
    exist and which the ledger does not list, write a ledger row with note
    'backfill' - after a pg_dump of the touched schemas (or the operator's
    attestation that none was taken). Returns what happened, per file."""
    root = Path(root)
    ensure_schema(conn, root)
    migrations = list_migrations(root)
    known = {r["name"] for r in applied(conn)}
    existing = existing_objects(conn)
    report = {"already_in_ledger": [], "backfilled": [], "partial": [], "absent": [],
              "unverifiable": [], "dump": None, "attested_no_dump": False, "schemas": []}
    candidates = []
    for m in migrations:
        if m["name"] in known:
            report["already_in_ledger"].append(m["name"])
            continue
        state = presence(m, existing)
        if state == "present":
            candidates.append(m)
        else:
            report[state].append(m["name"])
    if not candidates:
        return report
    report["schemas"] = touched_schemas(candidates)
    note = "backfill"
    if dump:
        if not which("pg_dump") or not dsn:
            if not attest_no_dump:
                raise LedgerError(
                    "no pg_dump on PATH or no DSN for it: the backfill takes a dump of "
                    f"{', '.join(report['schemas'])} first. Install pg_dump and pass the DSN, "
                    "or attest that no dump is taken (--no-dump)")
            report["attested_no_dump"] = True
            note = f"backfill; attested: no dump taken by {applied_by}"
        else:
            stamp = (at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
            out = Path(dump_dir or root / "restore-output") / f"ledger-backfill-{stamp}.dump"
            report["dump"] = pg_dump(dsn, report["schemas"], out, runner=runner, which=which)
            note = f"backfill; dump {report['dump']['path']} sha256 {report['dump']['sha256'][:12]}"
    else:
        if not attest_no_dump:
            raise LedgerError("dump=False needs attest_no_dump=True: say so, on the record")
        report["attested_no_dump"] = True
        note = f"backfill; attested: no dump taken by {applied_by}"
    for m in candidates:
        this_note = note
        if m["name"] == SCHEMA_FILE:
            this_note = "applied by the ledger backfill run"
        record(conn, m["name"], m["sha256"], applied_by, this_note)
        report["backfilled"].append(m["name"])
    conn.commit()
    return report


def status(root: Path, conn: Any = None) -> list[dict]:
    """Every file with its checksum and, when a connection is given, its
    ledger row: the evidence behind the Migration ledger row."""
    rows = list_migrations(root)
    ledger = {r["name"]: r for r in applied(conn)} if conn is not None else {}
    out = []
    for m in rows:
        entry = ledger.get(m["name"])
        if conn is None:
            state = "unknown"
        elif entry is None:
            state = "not in the ledger"
        elif entry["checksum"] != m["sha256"]:
            state = "checksum differs"
        else:
            state = "applied"
        out.append({"name": m["name"], "sha256": m["sha256"], "bytes": m["bytes"],
                    "tables": m["tables"], "schemas": m["schemas"], "roles": m["roles"],
                    "ledger": state, "applied_at": str(entry["applied_at"]) if entry else "",
                    "applied_by": entry["applied_by"] if entry else "",
                    "note": entry["note"] if entry else ""})
    return out
# Made by Ryan Gomez & Co. Inc.
