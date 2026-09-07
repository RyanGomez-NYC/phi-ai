# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""What each system holds.

No system holds the whole record; each holds a different part of it. The
platform can answer two of the three questions honestly from what it has:
what the PHI AI store itself holds, by type and per chart (the reader),
and what each target has RECEIVED from this deployment's runs (the run
ledger). What a source holds beyond what was read from it is that source's
to say - a live count needs a connection, and the screen says "not
connected" rather than inventing one.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping


def store_holdings(reader, charts: Iterable[dict] = ()) -> dict:
    """The PHI AI store's own holdings: by resource type, and per chart in
    the set."""
    try:
        st = reader.stats()
        by_type = dict(getattr(st, "resource_type_counts", {}) or {})
        total = int(getattr(st, "total_resources", 0) or 0)
        n_charts = int(getattr(st, "distinct_patients", 0) or 0)
    except Exception:
        by_type, total, n_charts = {}, 0, 0
    per_chart = []
    for ch in charts:
        counts: dict[str, int] = defaultdict(int)
        try:
            for r in reader.resources_for_patient(ch["ref"]) or []:
                rt = r.get("resource_type") if isinstance(r, dict) else None
                if rt:
                    counts[str(rt)] += 1
        except Exception:
            pass
        per_chart.append({"ref": ch["ref"], "name": ch.get("name", ch["ref"]),
                          "by_type": dict(sorted(counts.items()))})
    return {"by_type": dict(sorted(by_type.items())), "total": total, "charts": n_charts,
            "per_chart": per_chart}


def delivered_holdings(runs: Iterable[Mapping]) -> dict[str, dict[str, int]]:
    """target system -> resource type -> records written, summed over every
    step of every run that actually wrote. 'Decided, not written' steps
    contribute nothing: a target holds what it received, not what was
    allowed."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for run in runs:
        for step in run.get("steps", []) or []:
            if step.get("direction") != "deliver" or not step.get("written"):
                continue
            target = str(step.get("system", ""))
            types = step.get("types") or {}
            if isinstance(types, Mapping) and types:
                for rt, n in types.items():
                    out[target][str(rt)] += int(n or 0)
            else:
                out[target]["records"] += int(step.get("moved") or 0)
    return {k: dict(sorted(v.items())) for k, v in out.items()}
