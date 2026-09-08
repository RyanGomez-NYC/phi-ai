# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
The updater: the one service that holds the docker socket.

    python -m core.components.updater          # the service loop
    docker compose --profile updater up -d     # as a compose service

The web process cannot replace itself (proposal §9), so direct image
releases run here: the loop watches the journal's open job and, for a
confirmed direct job on the Running image row, runs the five steps
through the same Journal the screen reads - verify the release's signed
manifest, record the previous digest as the point of return, pull the
digest and `compose up -d` it, run the healthcheck, and on any failure
bring the previous digest back up and verify again.

Every subprocess call goes through an injectable runner so the tests run
without docker. A deployment that refuses the socket runs in guided mode:
same steps, the screen prints the commands.

Signing (proposal §5, §14 decision 3): the workstation signs
MANIFEST.sha256 with an ed25519 private key that lives on the operator's
machine only; the PUBLIC key ships in the tree at
config/release_signing.pub, and this service refuses an unsigned or
altered release before pulling a layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("phi-ai.components.updater")

ENV_IMAGE_VAR = "PHI_AI_IMAGE"
PUBKEY_FILE = Path("config") / "release_signing.pub"
MANIFEST_NAME = "MANIFEST.sha256"
SIGNATURE_NAME = "MANIFEST.sha256.sig"
UPDATER_ACTOR = "updater"


class UpdaterError(RuntimeError):
    """A docker step that failed; the message is the evidence."""


class SignatureError(UpdaterError):
    """The release is unsigned, altered, or signed by another key."""


# ---------------------------------------------------------------------------
# Signing: ed25519 over MANIFEST.sha256
# ---------------------------------------------------------------------------

def keygen(private_path: Path) -> str:
    """Write a new ed25519 private key (PEM, mode 0600) and return the
    public key PEM to put in the tree. Never call this into the repo."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    private_path = Path(private_path)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(pem)
    os.chmod(private_path, 0o600)
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def sign_file(path: Path, private_path: Path, sig_path: Optional[Path] = None) -> Path:
    """Detached ed25519 signature over the file's bytes (raw 64 bytes)."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
    sig = key.sign(Path(path).read_bytes())
    out = Path(sig_path) if sig_path else Path(str(path) + ".sig")
    out.write_bytes(sig)
    return out


def verify_file(path: Path, sig_path: Path, pubkey_path: Path) -> dict:
    """Raises SignatureError unless sig_path is pubkey's signature over
    path's bytes. Returns the file's sha256 as the evidence."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    path, sig_path, pubkey_path = Path(path), Path(sig_path), Path(pubkey_path)
    for p, what in ((path, "manifest"), (sig_path, "signature"), (pubkey_path, "public key")):
        if not p.is_file():
            raise SignatureError(f"no {what} at {p}")
    try:
        pub = serialization.load_pem_public_key(pubkey_path.read_bytes())
    except (ValueError, TypeError) as exc:
        raise SignatureError(f"{pubkey_path} is not a PEM public key: {exc}") from None
    data = path.read_bytes()
    try:
        pub.verify(sig_path.read_bytes(), data)
    except InvalidSignature:
        raise SignatureError(f"{sig_path.name} is not {pubkey_path.name}'s signature over {path.name}: "
                             "the release is altered, unsigned, or signed by another key") from None
    return {"manifest": str(path), "sha256": hashlib.sha256(data).hexdigest(), "verified": True,
            "public_key": str(pubkey_path)}


# ---------------------------------------------------------------------------
# The updater
# ---------------------------------------------------------------------------

class Updater:
    """docker compose, through one injectable runner."""

    def __init__(self, runner: Callable = subprocess.run, compose_file: Path = Path("docker-compose.yml"),
                 env_file: Path = Path(".env"), *, web_service: str = "web",
                 pubkey_path: Optional[Path] = None, timeout: int = 900):
        self._runner = runner
        self.compose_file = Path(compose_file)
        self.env_file = Path(env_file)
        self.web_service = web_service
        self.pubkey_path = Path(pubkey_path) if pubkey_path else self.compose_file.parent / PUBKEY_FILE
        self.timeout = timeout

    # ---- plumbing ----------------------------------------------------------

    def _run(self, argv: list[str]) -> dict:
        try:
            run = self._runner(argv, capture_output=True, text=True, check=False, timeout=self.timeout)
        except FileNotFoundError as exc:
            raise UpdaterError(f"{argv[0]} is not on PATH: {exc}") from None
        except subprocess.TimeoutExpired:
            raise UpdaterError(f"{' '.join(argv[:3])} timed out after {self.timeout}s") from None
        result = {"command": list(argv), "returncode": getattr(run, "returncode", 1),
                  "stdout": (getattr(run, "stdout", "") or "")[-2000:],
                  "stderr": (getattr(run, "stderr", "") or "")[-2000:]}
        if result["returncode"] != 0:
            raise UpdaterError(f"{' '.join(argv[:4])} exited {result['returncode']}: "
                               f"{result['stderr'].strip()[-400:] or result['stdout'].strip()[-400:]}")
        return result

    def _compose(self, *args: str) -> list[str]:
        return ["docker", "compose", "-f", str(self.compose_file), "--env-file", str(self.env_file), *args]

    # ---- the env file: the digest pin ------------------------------------------

    def current_digest(self) -> Optional[str]:
        """PHI_AI_IMAGE from the env file: the digest compose was told to run."""
        try:
            text = self.env_file.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(ENV_IMAGE_VAR + "="):
                value = stripped[len(ENV_IMAGE_VAR) + 1:].strip().strip('"').strip("'")
                return value or None
        return None

    def write_digest(self, digest: str) -> Optional[str]:
        """Pin PHI_AI_IMAGE=<digest> in the env file (replacing the line or
        appending it) and return the previous value."""
        previous = self.current_digest()
        try:
            lines = self.env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        out, done = [], False
        for line in lines:
            if line.strip().startswith(ENV_IMAGE_VAR + "="):
                if not done:
                    out.append(f"{ENV_IMAGE_VAR}={digest}")
                    done = True
                continue
            out.append(line)
        if not done:
            out.append(f"{ENV_IMAGE_VAR}={digest}")
        self.env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
        return previous

    # ---- the five steps' pieces -----------------------------------------------

    def verify_signature(self, manifest_path: Path, sig_path: Path, pubkey_path: Optional[Path] = None) -> dict:
        return verify_file(manifest_path, sig_path, pubkey_path or self.pubkey_path)

    def prechecks(self, target: str, release_dir: Path) -> dict:
        """Before anything is touched: the release directory carries a
        signed manifest whose BUILD.json names the target digest."""
        release_dir = Path(release_dir)
        manifest, sig = release_dir / MANIFEST_NAME, release_dir / SIGNATURE_NAME
        signature = self.verify_signature(manifest, sig)
        stamp_path = release_dir / "BUILD.json"
        try:
            stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SignatureError(f"no readable BUILD.json beside the manifest: {exc}") from None
        digest = stamp.get("image_digest")
        if digest != target:
            raise SignatureError(f"the signed release names image {digest!r}, not the job's target {target!r}")
        expected = hashlib.sha256(stamp_path.read_bytes()).hexdigest()
        listed = [ln for ln in manifest.read_text(encoding="utf-8").splitlines() if ln.endswith("  BUILD.json")]
        if not listed or not listed[0].startswith(expected):
            raise SignatureError("BUILD.json is not the one the signed manifest lists")
        return {"signature": signature, "release": stamp.get("release"), "digest": digest}

    def pull(self, digest: str) -> dict:
        return self._run(["docker", "pull", digest])

    def up(self, digest: str) -> dict:
        """Pin the digest in the env file and bring the services up on it.
        --no-build: a release is pulled by digest, never built from a context
        this container does not have."""
        previous = self.write_digest(digest)
        result = self._run(self._compose("up", "-d", "--remove-orphans", "--no-build"))
        result.update(digest=digest, previous_digest=previous, env_file=str(self.env_file))
        return result

    def healthcheck(self) -> dict:
        result = self._run(self._compose("exec", "-T", self.web_service, "python", "-m", "core.healthcheck"))
        result["green"] = True
        return result

    def rollback(self, previous_digest: str) -> dict:
        if not previous_digest:
            raise UpdaterError("no previous digest to roll back to")
        result = self.up(previous_digest)
        result["rolled_back_to"] = previous_digest
        return result


# ---------------------------------------------------------------------------
# The service loop
# ---------------------------------------------------------------------------

def run_job(journal: Any, job: dict, updater: Updater, release_dir: Path) -> dict:
    """Drive one confirmed direct Running-image job through its steps."""
    job_id = job["id"]
    if job["status"] == "confirmed":
        try:
            evidence = updater.prechecks(job["target"], release_dir)
        except UpdaterError as exc:
            # Nothing was touched: the job fails closed at Back up, with the reason.
            journal.advance(job_id, "Back up", "failed", UPDATER_ACTOR, evidence={"precheck": str(exc)})
            return journal.finish(job_id, UPDATER_ACTOR)
        log.info("prechecks passed for %s: release %s", job_id, evidence.get("release"))
    while True:
        job = journal.get_job(job_id)
        status = job["status"]
        if status in ("done", "failed", "recovered"):
            break
        if status == "verify_failed":
            job = journal.rollback(job_id, UPDATER_ACTOR)
            continue
        nxt = journal.next_step(job_id)
        if nxt is None:
            break
        journal.advance(job_id, nxt, "ok", UPDATER_ACTOR)
    return journal.finish(job_id, UPDATER_ACTOR)


def _journal_from_env(root: Path, updater: Updater):
    from core.components.journal import Journal
    from core.config.settings import ConfigError, Settings
    from core.db.connection import connect

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise UpdaterError(f"the updater needs the index database for the journal: {exc}") from None
    if not settings.db_reader_username:
        raise UpdaterError("PHI_AI_DB_READER_USERNAME is not set; the journal lives in the index")
    return Journal(connection_factory=lambda: connect(settings, settings.db_reader_username),
                   root=root, updater=updater)


def main(argv: Optional[list[str]] = None, *, journal: Any = None, updater: Optional[Updater] = None) -> int:
    ap = argparse.ArgumentParser(description="PHI AI updater: runs direct image releases from the journal")
    ap.add_argument("--root", type=Path, default=Path(os.environ.get("PHI_AI_ROOT", ".")))
    ap.add_argument("--compose-file", type=Path, default=None)
    ap.add_argument("--env-file", type=Path, default=None)
    ap.add_argument("--release-dir", type=Path, default=None,
                    help="where a release's BUILD.json, MANIFEST.sha256 and .sig are dropped (default <root>/releases; "
                             "not release/, which collides with the RELEASE file on case-insensitive filesystems)")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between looks at the journal")
    ap.add_argument("--once", action="store_true", help="look once and exit (tests, cron)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    updater = updater or Updater(compose_file=args.compose_file or root / "docker-compose.yml",
                                 env_file=args.env_file or root / ".env")
    release_dir = args.release_dir or root / "releases"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        journal = journal or _journal_from_env(root, updater)
    except UpdaterError as exc:
        print(f"updater: {exc}", file=sys.stderr)
        return 2
    while True:
        job = journal.open_job()
        if job and job["component"] == "running_image" and job["mode"] == "direct" \
                and job["status"] in ("confirmed", "running", "verify_failed", "recovering"):
            log.info("job %s: %s -> %s (%s)", job["id"], job["component"], job["target"], job["status"])
            try:
                final = run_job(journal, job, updater, release_dir)
                log.info("job %s closed as %s", job["id"], final["status"])
            except Exception as exc:  # noqa: BLE001 - the loop must survive one bad job
                log.error("job %s: %s", job["id"], exc)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
