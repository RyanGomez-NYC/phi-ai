#!/usr/bin/env python3
# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The workstation CLI for the Components screen (proposal §11).

    scripts/components.py build [--image-digest REF] [--sign KEYFILE] [--out DIR]
        BUILD.json, MANIFEST.sha256 over every git-tracked file, and with
        --sign the ed25519 signature MANIFEST.sha256.sig.
    scripts/components.py show [--json]
        The screen's view, in the terminal.
    scripts/components.py check --offline [--out FILE]
        components.manifest.json from what can be known without the
        network. The online checks (advisories, upstream releases, vendor
        pages, provider deprecations, terminology publishers) are slice 4
        and NOT YET IMPLEMENTED: `check` without --offline says so and
        exits 2. Nothing is faked.
    scripts/components.py ledger --dsn DSN [--backfill] [--applied-by WHO] [--no-dump]
        The migration ledger against the index; --backfill writes the
        one-time rows after a pg_dump of the touched schemas.
    scripts/components.py vendored [--check]
        Regenerate VENDORED.json from the tree.
    scripts/components.py keygen --out KEYFILE
        A new release-signing key pair: the PRIVATE key to KEYFILE (mode
        0600, outside the repository), the PUBLIC key to
        config/release_signing.pub.
    scripts/components.py verify [--dir DIR]
        Verify MANIFEST.sha256.sig against config/release_signing.pub.

Stdlib plus what requirements.txt already has (cryptography, PyYAML,
psycopg). Every value written comes from a file or a command it names.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.components import build, ledger, manifest, vendored  # noqa: E402
from core.components.registry import CADENCE_DAYS, load_members, read_all, summary  # noqa: E402


def die(msg: str, code: int = 2) -> int:
    print(f"components: {msg}", file=sys.stderr)
    return code


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- build ---------------------------------------------------------------

def tracked_files(root: Path) -> list[str]:
    run = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=False)
    if run.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {run.stderr.strip()} (the manifest lists what git tracks)")
    return sorted(p for p in run.stdout.split("\0") if p)


def write_manifest(root: Path, out_dir: Path, stamp_path: Path) -> Path:
    lines = []
    for rel in tracked_files(root):
        path = root / rel
        if path.is_file():
            lines.append(f"{sha256_file(path)}  {rel}")
    lines.append(f"{sha256_file(stamp_path)}  {build.STAMP_FILE}")
    out = out_dir / "MANIFEST.sha256"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def cmd_build(args: argparse.Namespace) -> int:
    root: Path = args.root
    out_dir: Path = args.out or root
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = build.write_stamp(root, out_dir / build.STAMP_FILE, image_digest=args.image_digest)
    print(f"wrote {out_dir / build.STAMP_FILE}: release {stamp['release']}, commit {build.short(stamp['commit'])}, "
          f"branch {stamp['branch']}" + (f", image {stamp['image_digest']}" if stamp.get("image_digest") else ""))
    try:
        man = write_manifest(root, out_dir, out_dir / build.STAMP_FILE)
    except RuntimeError as exc:
        return die(str(exc))
    print(f"wrote {man} ({sum(1 for _ in man.open())} files)")
    if args.sign:
        from core.components.updater import sign_file
        sig = sign_file(man, args.sign)
        print(f"wrote {sig} (ed25519, key {args.sign})")
    else:
        print("not signed: pass --sign KEYFILE to write MANIFEST.sha256.sig (the updater refuses an unsigned release)")
    return 0


# ---- show ----------------------------------------------------------------

def _readings(root: Path):
    from core.components import members
    load_members()
    return read_all(members.context(root))


def cmd_show(args: argparse.Namespace) -> int:
    readings = _readings(args.root)
    if args.json:
        out = []
        for comp, r in readings:
            out.append({"key": comp.key, "group": comp.group, "name": comp.name, "mode": comp.mode, "state": r.state,
                        "running": r.running.__dict__, "built": r.built.__dict__, "latest": r.latest.__dict__,
                        "note": r.note, "evidence": {"columns": r.evidence.columns, "rows": r.evidence.rows}})
        print(json.dumps(out, indent=2, default=str))
        return 0
    cards = summary(readings)
    man = manifest.load(args.root)
    print(f"current {cards['current']} · behind or drifted {cards['behind']} · unknown {cards['unknown']}")
    if man:
        age = manifest.age(man)
        print(f"manifest produced {man.get('produced_at')} by {man.get('produced_by')} on {man.get('host')}"
              + (f" ({age.days} days old{'; STALE' if manifest.stale(man) else ''})" if age else ""))
    else:
        print("no components.manifest.json: run scripts/components.py check --offline")
    group = None
    for comp, r in readings:
        if comp.group != group:
            group = comp.group
            from core.components.registry import GROUPS
            print(f"\n{group}. {GROUPS[group]}")
        print(f"  {comp.key:18} {r.state:8} {comp.mode:7} running: {r.running.value[:70]}")
        print(f"  {'':18} {'':8} {'':7} built:   {r.built.value[:70]}")
        print(f"  {'':18} {'':8} {'':7} latest:  {r.latest.value[:70]}")
        if r.note:
            print(f"  {'':18} {'':8} {'':7} note:    {r.note[:90]}")
        if args.evidence:
            print(f"  {'':18} {'':8} {'':7} {' | '.join(r.evidence.columns)}")
            for row in r.evidence.rows:
                print(f"  {'':18} {'':8} {'':7}   {' | '.join(row)}")
    return 0


# ---- check ---------------------------------------------------------------

ONLINE_CHECKS = ("advisories against requirements.lock", "upstream releases of the base image, Terraform and providers",
                 "vendor documentation page change detection", "provider model lists and deprecations",
                 "terminology publisher pages", "upstream versions of the vendored front-end")


def offline_components(root: Path, at: datetime) -> dict:
    """What the workstation can record without the network: tree facts and
    its own disk. Every entry is marked offline; the readers treat an
    offline entry as unknown for any fact that needs upstream."""
    comps: dict[str, dict] = {}
    facts = build.git_facts(root)
    rel = build.read_release(root) or build.UNKNOWN
    comps["build_stamp"] = manifest.entry(f"{rel} @ {build.short(facts['commit'])}", facts["source"],
                                         fetched_at=at, offline=True, commit=facts["commit"], branch=facts["branch"])
    stamp = build.read_stamp(root)
    if stamp and stamp.get("image_digest"):
        comps["running_image"] = manifest.entry(stamp["image_digest"], "BUILD.json image_digest on the workstation",
                                                fetched_at=at, offline=True, release=stamp.get("release"))
    lock = root / "requirements.lock"
    if lock.is_file():
        n = sum(1 for ln in lock.read_text(encoding="utf-8").splitlines() if "==" in ln and not ln.startswith(("#", " ")))
        comps["python_pins"] = manifest.entry(f"{n} pins as locked; advisories not checked (offline)", "requirements.lock",
                                              fetched_at=at, sha256=sha256_file(lock), offline=True,
                                              lock_sha256=sha256_file(lock), advisories=None)
    dockerfile = root / "Dockerfile"
    if dockerfile.is_file():
        first = [ln for ln in dockerfile.read_text(encoding="utf-8").splitlines() if ln.startswith("FROM ")]
        if first:
            comps["runtimes"] = manifest.entry(first[0].split()[1], "Dockerfile FROM (upstream not checked: offline)",
                                               fetched_at=at, offline=True)
    versions = root / "deploy" / "aws" / "versions.tf"
    if versions.is_file():
        from core.components.members import _tf_pins
        req, providers = _tf_pins(versions.read_text(encoding="utf-8"))
        comps["infra_pins"] = manifest.entry(
            f"terraform {req}; " + "; ".join(f"{n} {v}" for n, _, v in providers) + " (provider releases not checked: offline)",
            "deploy/aws/versions.tf", fetched_at=at, offline=True,
            providers=[{"name": n, "source": s, "constraint": v} for n, s, v in providers])
    try:
        from core.components.members import _default_model
        from core.web.platform_state import PlatformState
        ids = sorted({m["model_id"] for m in PlatformState().list_models() if m.get("model_id")})
        default = _default_model(root)
        comps["model_catalogue"] = manifest.entry(
            f"{len(ids)} shipped model ids; provider deprecations not checked (offline)",
            "core/web/platform_state.py + core/assistant/config.py", fetched_at=at, offline=True,
            model_ids=ids, default_model=default, deprecated=None)
    except Exception as exc:  # noqa: BLE001 - the manifest records what it could read
        comps["model_catalogue"] = manifest.entry("model ids could not be read", f"error: {type(exc).__name__}",
                                                  fetched_at=at, offline=True)
    keys = sorted(root.glob("epic_*.pem")) + sorted((root / "config").glob("*.pem")) + sorted((root / "config").glob("*jwks*.json"))
    ages = []
    for k in keys:
        mtime = datetime.fromtimestamp(k.stat().st_mtime, tz=timezone.utc)
        ages.append({"file": k.relative_to(root).as_posix(), "age_days": (at - mtime).days,
                     "due": (mtime + timedelta(days=CADENCE_DAYS["keys"])).date().isoformat()})
    # Always an entry: a tree that carries no key file says so, rather than
    # leaving the row to guess between "not checked" and "nothing to check".
    comps["key_ages"] = manifest.entry(
        f"{len(ages)} key files; oldest {max(a['age_days'] for a in ages)} days" if ages
        else "no key files in the tree (epic_*.pem, config/*.pem, config/*jwks*.json)",
        "file mtimes on the workstation (ages only)", fetched_at=at, offline=True, keys=ages)
    recorded = vendored.load(root)
    if recorded:
        comps["vendored_frontend"] = manifest.entry(
            f"{len(recorded['files'])} files as recorded; upstream not checked (offline)", "VENDORED.json",
            fetched_at=at, offline=True)
    try:
        from core.terminology.loader import SOURCES
        comps["terminology"] = manifest.entry(f"{len(SOURCES)} sources declared; publisher pages not checked (offline)",
                                              "core/terminology/loader.py SOURCES", fetched_at=at, offline=True)
    except Exception as exc:  # noqa: BLE001
        comps["terminology"] = manifest.entry("sources could not be read", f"error: {type(exc).__name__}",
                                              fetched_at=at, offline=True)
    try:
        from core.fhir.emr_profiles import PROFILES
        comps["emr_profiles"] = manifest.entry(f"{len(PROFILES)} profiles; vendor pages not checked (offline)",
                                               "core/fhir/emr_profiles.py PROFILES", fetched_at=at, offline=True)
    except Exception as exc:  # noqa: BLE001
        comps["emr_profiles"] = manifest.entry("profiles could not be read", f"error: {type(exc).__name__}",
                                               fetched_at=at, offline=True)
    return comps


def cmd_check(args: argparse.Namespace) -> int:
    if not args.offline:
        print("components check (online) is NOT YET IMPLEMENTED - slice 4 of the proposal. It would fetch:",
              file=sys.stderr)
        for item in ONLINE_CHECKS:
            print(f"  - {item}", file=sys.stderr)
        print("Run `scripts/components.py check --offline` for what can be known without the network.", file=sys.stderr)
        return 2
    at = datetime.now(timezone.utc)
    comps = offline_components(args.root, at)
    man = manifest.build(comps, produced_by=f"scripts/components.py check --offline ({getpass.getuser()})",
                         host=socket.gethostname(), at=at)
    out = Path(args.out) if args.out else args.root / manifest.FILE
    out.write_text(json.dumps(man, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(comps)} entries, all offline (upstream not checked; online checks are slice 4)")
    return 0


# ---- ledger --------------------------------------------------------------

def cmd_ledger(args: argparse.Namespace) -> int:
    try:
        import psycopg
    except ImportError:
        return die("psycopg is not installed in this environment")
    try:
        conn = psycopg.connect(args.dsn)
    except Exception as exc:  # noqa: BLE001
        return die(f"could not connect: {type(exc).__name__}: {exc}")
    try:
        if args.backfill:
            try:
                report = ledger.backfill(conn, args.applied_by, root=args.root, dsn=args.dsn,
                                         dump=not args.no_dump, attest_no_dump=args.no_dump,
                                         dump_dir=args.dump_dir)
            except ledger.LedgerError as exc:
                return die(str(exc))
            for key in ("backfilled", "already_in_ledger", "partial", "absent", "unverifiable"):
                print(f"{key:18} {', '.join(report[key]) or '-'}")
            if report["dump"]:
                print(f"dump               {report['dump']['path']} (sha256 {report['dump']['sha256'][:12]}; "
                      f"schemas {', '.join(report['dump']['schemas'])})")
            if report["attested_no_dump"]:
                print("dump               none: attested by the operator (--no-dump)")
            return 0
        rows = ledger.status(args.root, conn)
        width = max(len(r["name"]) for r in rows)
        for r in rows:
            print(f"{r['name'].ljust(width)}  {r['sha256'][:12]}  {r['ledger']:18} {r['applied_at']} {r['applied_by']}")
        return 0
    finally:
        conn.close()


# ---- vendored, keygen, verify -------------------------------------------

def cmd_vendored(args: argparse.Namespace) -> int:
    if args.check:
        current = vendored.load(args.root)
        fresh = {"generated_by": "scripts/components.py vendored", "files": vendored.scan(args.root)}
        if current == fresh:
            print(f"{vendored.FILE} is current ({len(fresh['files'])} files)")
            return 0
        print(f"{vendored.FILE} differs from a fresh scan; rerun without --check", file=sys.stderr)
        return 1
    out = vendored.write(args.root)
    print(f"wrote {out} ({len(vendored.scan(args.root))} files)")
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    from core.components.updater import PUBKEY_FILE, keygen
    private = Path(args.out).expanduser()
    try:
        private.resolve().relative_to(args.root.resolve())
    except ValueError:
        pass
    else:
        return die(f"{private} is inside the repository; the private key lives on the operator's machine only")
    if private.exists() and not args.force:
        return die(f"{private} exists; pass --force to overwrite it")
    pub = keygen(private)
    pub_path = Path(args.pubkey_out) if args.pubkey_out else args.root / PUBKEY_FILE
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.write_text(pub, encoding="utf-8")
    print(f"wrote private key {private} (mode 0600; never commit it)\nwrote public key {pub_path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from core.components.updater import PUBKEY_FILE, SignatureError, verify_file
    d = Path(args.dir) if args.dir else args.root
    try:
        result = verify_file(d / "MANIFEST.sha256", d / "MANIFEST.sha256.sig", args.pubkey or args.root / PUBKEY_FILE)
    except SignatureError as exc:
        return die(str(exc), 1)
    print(f"verified {result['manifest']} (sha256 {result['sha256'][:12]}) against {result['public_key']}")
    return 0


# ---- main ----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    sub = ap.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="write BUILD.json and MANIFEST.sha256, optionally signed")
    b.add_argument("--out", type=Path, default=None, help="directory to write into (default: the root)")
    b.add_argument("--image-digest", default=None, help="the image reference with digest, repo@sha256:...")
    b.add_argument("--sign", type=Path, default=None, help="ed25519 private key file (PEM) to sign the manifest with")
    b.set_defaults(fn=cmd_build)

    s = sub.add_parser("show", help="render the screen's view in the terminal")
    s.add_argument("--json", action="store_true")
    s.add_argument("--evidence", action="store_true", help="print every evidence row")
    s.set_defaults(fn=cmd_show)

    c = sub.add_parser("check", help="write components.manifest.json")
    c.add_argument("--offline", action="store_true", help="record what can be known without the network")
    c.add_argument("--out", default=None)
    c.set_defaults(fn=cmd_check)

    l = sub.add_parser("ledger", help="the migration ledger against the index")
    l.add_argument("--dsn", required=True, help="a libpq DSN or URI with the credential the ledger may write with")
    l.add_argument("--backfill", action="store_true", help="write the one-time backfill rows")
    l.add_argument("--applied-by", default=getpass.getuser())
    l.add_argument("--no-dump", action="store_true", help="attest that no pg_dump is taken (recorded in the note)")
    l.add_argument("--dump-dir", type=Path, default=None, help="where the pg_dump goes (default: restore-output/)")
    l.set_defaults(fn=cmd_ledger)

    v = sub.add_parser("vendored", help="regenerate VENDORED.json from the tree")
    v.add_argument("--check", action="store_true", help="exit 1 if VENDORED.json differs from a fresh scan")
    v.set_defaults(fn=cmd_vendored)

    k = sub.add_parser("keygen", help="a new release-signing key pair")
    k.add_argument("--out", required=True, help="where the PRIVATE key goes (outside the repository)")
    k.add_argument("--pubkey-out", default=None, help="where the public key goes (default: config/release_signing.pub)")
    k.add_argument("--force", action="store_true")
    k.set_defaults(fn=cmd_keygen)

    vf = sub.add_parser("verify", help="verify MANIFEST.sha256.sig against the tree's public key")
    vf.add_argument("--dir", default=None, help="directory holding MANIFEST.sha256 and .sig (default: the root)")
    vf.add_argument("--pubkey", type=Path, default=None)
    vf.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    args.root = Path(args.root).resolve()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
