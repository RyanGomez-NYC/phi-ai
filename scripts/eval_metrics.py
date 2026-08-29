# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Run the §10 validation metrics over the synthetic corpus.

    python scripts/eval_metrics.py             # layer4 + layer2 when present
    python scripts/eval_metrics.py --no-layer2 # hand-authored fixtures only

Prints core/rag/eval.py's report. Layer 2 (testdata/layer2/, generated
by scripts/generate_corpus.py) is included automatically when present
and silently absent when not — regenerating it is one command, and its
absence changes coverage, not correctness. Every number printed is a
LOWER BOUND ON ERROR per SPEC §7.7: synthetic narrative is materially
cleaner than production narrative and these figures do not transfer to
real data without the §10 pilot.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.governance.segmentation import CategoryValueSets  # noqa: E402
from core.rag.eval import run_all  # noqa: E402
from core.rag.pipeline import serialize_corpus  # noqa: E402

LAYER4 = REPO_ROOT / "tests" / "fixtures" / "layer4"
LAYER2 = REPO_ROOT / "testdata" / "layer2"

#: Recall probes per patient are capped: the probe set is quadratic in
#: chunk count and a 1,500-resource Synthea patient proves nothing more
#: at probe 1,500 than at probe 50.
MAX_RECALL_PROBES_PER_PATIENT = 50


def load_layer4() -> dict[str, dict]:
    resources = {}
    for path in sorted(LAYER4.glob("*.json")):
        if path.name == "MANIFEST.json":
            continue
        resources[f"fixtures/{path.name}"] = json.loads(path.read_text())
    return resources


def load_layer2(limit_bundles: int | None = None) -> dict[str, dict]:
    resources: dict[str, dict] = {}
    if not LAYER2.exists():
        return resources
    bundles = [p for p in sorted(LAYER2.glob("*.json")) if p.name != "MANIFEST.json"]
    if limit_bundles:
        bundles = bundles[:limit_bundles]
    for path in bundles:
        bundle = json.loads(path.read_text())
        for i, entry in enumerate(bundle.get("entry", [])):
            resource = entry.get("resource")
            if isinstance(resource, dict):
                resources[f"layer2/{path.stem}#{i}"] = resource
    return resources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-layer2", action="store_true")
    parser.add_argument("--bundles", type=int, default=None,
                        help="cap Layer-2 bundles for a faster run")
    args = parser.parse_args()

    resources = load_layer4()
    layer4_count = len(resources)
    if not args.no_layer2:
        resources.update(load_layer2(args.bundles))

    chunks = serialize_corpus(resources, CategoryValueSets())

    report = run_all(
        resources,
        chunks,
        anchor=date(2026, 8, 21),
        max_recall_probes_per_patient=MAX_RECALL_PROBES_PER_PATIENT,
    )

    print(
        f"corpus: {layer4_count} layer4 fixtures"
        + ("" if args.no_layer2 else f" + {len(resources) - layer4_count} layer2 resources")
        + f" -> {len(chunks)} chunks"
    )
    print(report.render())

    # Exit nonzero on the two target-zero metrics so this can gate a
    # release checklist run.
    failed = report.status_inversions > 0 or report.attribution_false_negatives > 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
