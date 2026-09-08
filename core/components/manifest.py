# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
components.manifest.json: "latest known", written by the workstation CLI.

A PHI host never phones home, so every fact that needs the network
(advisories, upstream releases, vendor pages, provider deprecations,
terminology publishers) is fetched on the operator's workstation by
scripts/components.py check and carried to the host in this file:

    {
      "produced_at": ISO-8601, "produced_by": who, "host": where,
      "components": {
        key: {"value": str, "source": str, "url": str|None,
              "fetched_at": ISO-8601, "sha256": str|None,
              "offline": bool, ...}
      }
    }

An entry marked offline was written without the network (check
--offline): it records what the workstation could read from the tree and
its own disk, and says nothing about upstream. The readers treat such an
entry as unknown for any fact that needs upstream - never as current.

The manifest's age is the advisories cadence (CADENCE_DAYS["advisories"],
14 days): past it the screen colours the manifest line amber.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.components.registry import CADENCE_DAYS, now

FILE = "components.manifest.json"


def load(root: Path) -> Optional[dict]:
    """The manifest under root, parsed; None when absent or unreadable."""
    try:
        data = json.loads((Path(root) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("components"), dict):
        return None
    return data


def parse_time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def produced_at(manifest: Optional[dict]) -> Optional[datetime]:
    return parse_time((manifest or {}).get("produced_at"))


def age(manifest: Optional[dict], at: Optional[datetime] = None) -> Optional[timedelta]:
    """How old the manifest is, or None when there is none or it is undated."""
    stamp = produced_at(manifest)
    if stamp is None:
        return None
    return (at or now()) - stamp


def stale(manifest: Optional[dict], days: int = CADENCE_DAYS["advisories"],
          at: Optional[datetime] = None) -> bool:
    """True when there is no manifest, it is undated, or it is older than
    `days`. Unknown is stale: never checked is never current."""
    delta = age(manifest, at)
    return delta is None or delta > timedelta(days=days)


def entry(value: str, source: str, *, url: Optional[str] = None,
          fetched_at: Optional[datetime] = None, sha256: Optional[str] = None,
          offline: bool = False, **extra) -> dict:
    """One component's entry, in the schema above."""
    row = {"value": str(value), "source": source, "url": url,
           "fetched_at": (fetched_at or now()).isoformat(timespec="seconds"),
           "sha256": sha256, "offline": bool(offline)}
    row.update(extra)
    return row


def is_offline(component_entry: Optional[dict]) -> bool:
    return bool((component_entry or {}).get("offline"))


def build(components: dict, *, produced_by: str, host: Optional[str] = None,
          at: Optional[datetime] = None) -> dict:
    return {"produced_at": (at or now()).isoformat(timespec="seconds"),
            "produced_by": produced_by, "host": host or socket.gethostname(),
            "components": dict(components)}


def write(root: Path, manifest: dict) -> Path:
    path = Path(root) / FILE
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
# Made by Ryan Gomez & Co. Inc.
