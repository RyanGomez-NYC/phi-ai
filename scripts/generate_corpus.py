# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Layer 2 synthetic corpus generation (docs/SPEC.md §7.2, docs/TESTDATA.md).

    python scripts/generate_corpus.py [--population 25] [--state Massachusetts]

Downloads the PINNED Synthea release, runs it with an EXPLICIT seed
(R3: Synthea's default seed is the wall clock — never rely on it),
post-processes every emitted resource to carry the R2 synthetic marker
(`meta.tag` HTEST — Synthea does not emit `meta.tag` itself, verified),
and writes a provenance MANIFEST.json recording generator, version, jar
SHA-256, seed, and the exact command line (R4).

OUTPUT IS NOT COMMITTED. The corpus lands in `testdata/layer2/`
(gitignored): reproducibility comes from (generator, pinned version,
seed), not from checking hundreds of megabytes of JSON into git — the
manifest IS the committed artifact. Tests that want Layer 2 skip when
the directory is absent, the same pattern the live-Postgres tests use.

Synthea's calibration is DIRECTIONAL, NOT QUANTITATIVE (§7.2 —
verified: the Synthea paper reports ~4000× national rates for
diabetes-related amputation). This corpus supplies volume and
structure. It supports no epidemiological claim, and nothing generated
here may be cited as one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Pinned per SPEC §7.2: v4.0.0 (5 Mar 2026), Apache-2.0.
SYNTHEA_VERSION = "v4.0.0"
SYNTHEA_JAR_URL = (
    "https://github.com/synthetichealth/synthea/releases/download/"
    f"{SYNTHEA_VERSION}/synthea-with-dependencies.jar"
)
#: R3: the explicit seed, passed as both -s (population) and -cs
#: (clinician). Date-shaped for legibility; the VALUE is what matters.
SEED = "20260821"

CACHE_DIR = REPO_ROOT / ".cache"
OUTPUT_DIR = REPO_ROOT / "testdata" / "layer2"

HTEST_TAG = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
    "code": "HTEST",
    "display": "test health data",
}


def _find_java() -> str:
    for candidate in ("/opt/homebrew/opt/openjdk/bin/java", "java"):
        if shutil.which(candidate) or Path(candidate).exists():
            return candidate
    raise SystemExit(
        "No Java runtime found. Install one (e.g. `brew install openjdk`) "
        "and re-run - Synthea is a JVM application."
    )


def _download_jar() -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    jar = CACHE_DIR / f"synthea-with-dependencies-{SYNTHEA_VERSION}.jar"
    if not jar.exists():
        print(f"downloading {SYNTHEA_JAR_URL} ...", flush=True)
        with urllib.request.urlopen(SYNTHEA_JAR_URL) as response, open(jar, "wb") as f:
            shutil.copyfileobj(response, f)
    return jar


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _mark_resource(resource: dict) -> None:
    """R2, applied in place. Idempotent - re-marking an already-marked
    resource changes nothing."""
    meta = resource.setdefault("meta", {})
    tags = meta.setdefault("tag", [])
    if not any(
        t.get("system") == HTEST_TAG["system"] and t.get("code") == HTEST_TAG["code"]
        for t in tags
    ):
        tags.append(dict(HTEST_TAG))


def _postprocess(fhir_dir: Path, out_dir: Path) -> tuple[int, int]:
    """Marks every resource in every bundle; returns (bundles, resources)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bundles = resources = 0
    for path in sorted(fhir_dir.glob("*.json")):
        bundle = json.loads(path.read_text())
        if bundle.get("resourceType") == "Bundle":
            for entry in bundle.get("entry", []):
                resource = entry.get("resource")
                if isinstance(resource, dict):
                    _mark_resource(resource)
                    resources += 1
        else:
            _mark_resource(bundle)
            resources += 1
        (out_dir / path.name).write_text(json.dumps(bundle, indent=2) + "\n")
        bundles += 1
    return bundles, resources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--population", type=int, default=25)
    parser.add_argument("--state", default="Massachusetts")
    args = parser.parse_args()

    java = _find_java()
    jar = _download_jar()
    jar_sha = _sha256(jar)

    workdir = CACHE_DIR / "synthea-run"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    command = [
        java,
        "-jar",
        str(jar),
        "-s",
        SEED,
        "-cs",
        SEED,
        "-p",
        str(args.population),
        "--exporter.fhir.use_us_core_ig=true",
        "--exporter.baseDirectory",
        str(workdir),
        args.state,
    ]
    print("running:", " ".join(command), flush=True)
    subprocess.run(command, check=True)

    fhir_dir = workdir / "fhir"
    if not fhir_dir.exists():
        raise SystemExit(f"Synthea produced no {fhir_dir}; aborting")

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    bundles, resources = _postprocess(fhir_dir, OUTPUT_DIR)

    manifest = {
        "fixture_set": "layer2-synthea",
        "generator": "synthea",
        "generator_version": SYNTHEA_VERSION,
        "jar_sha256": jar_sha,
        "seed": SEED,
        "command_line": " ".join(command[2:]),  # jar-relative, java path is host detail
        "calibration_sources": (
            "Synthea module library - DIRECTIONAL ONLY, no epidemiological "
            "claim supported (SPEC §7.2; Walonoski et al. 2018, "
            "doi:10.1093/jamia/ocx079)"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundles": bundles,
        "resources_marked": resources,
        "note": (
            "Corpus is NOT committed (reproducible from generator+version+"
            "seed per R3); this manifest is the committed record. Regenerate "
            "with scripts/generate_corpus.py."
        ),
    }
    manifest_path = OUTPUT_DIR / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    # The committed copy of the manifest lives next to the Layer 4 set so
    # the R4 record survives even though the corpus itself is ignored.
    committed = REPO_ROOT / "tests" / "fixtures" / "layer2.MANIFEST.json"
    committed.write_text(json.dumps(manifest, indent=2) + "\n")

    print(
        f"layer2 corpus: {bundles} bundles, {resources} resources marked, "
        f"manifest at {manifest_path} (committed copy: {committed})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
