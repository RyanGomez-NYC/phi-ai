# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI — Apache-2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""The EMR board: one row per vendor profile, with the vendor's own posture.

The demonstration's home page carries this board and the platform's overview
carries the same one, derived the same way - from the profiles, never typed.
Each row says in plain words what an exchange can do with that system: how it
reads (a bulk export, or one chart at a time), whether it accepts writes on
its certified surface and for which resource types, and whether the vendor
sells a write connector that extends that surface. What a system HOLDS is not
on the board: the platform is not connected to it for a count, and the
holdings screen says so rather than inventing one. What the board can say
about this deployment is whether the system is wired, and as what.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.fhir.commercial.registry import vendors_with_commercial_path
from core.orchestration.systems import systems

__all__ = ["BoardRow", "board", "board_summary", "count_word", "marks"]


@dataclass(frozen=True)
class BoardRow:
    key: str
    name: str
    mark: str
    reads: str            # "bulk export" | "one chart at a time"
    reads_kind: str       # "bulk" | "chart"
    writes: str           # what the certified surface accepts, in words
    writes_kind: str      # "writes" | "later" | "none"
    use: str              # how this deployment has it wired, in words


def _letters(name: str) -> str:
    words = name.split()
    if len(words) >= 2:
        return (words[0][:1] + words[1][:1]).upper()
    return name[:2].upper()


def marks(names: dict[str, str]) -> dict[str, str]:
    """Two letters per key: initials where the name has two words, else the
    first two letters. Two names that derive the same two letters (MEDITECH
    and MEDHOST, Netsmart and Nextech) stay tellable apart: each keeps its
    first letter and takes the first letter at which it differs from the
    others in the clash."""
    out = {k: _letters(n) for k, n in names.items()}
    by_mark: dict[str, list[str]] = {}
    for k, m in out.items():
        by_mark.setdefault(m, []).append(k)
    for clash in by_mark.values():
        if len(clash) < 2:
            continue
        flat = {k: "".join(names[k].split()).upper() for k in clash}
        for k in clash:
            me = flat[k]
            for i in range(1, len(me)):
                if all(len(flat[o]) <= i or flat[o][i] != me[i] for o in clash if o != k):
                    out[k] = me[0] + me[i]
                    break
    return out


def board(sources: Iterable[str] = (), targets: Iterable[str] = ()) -> tuple[BoardRow, ...]:
    """One row per profile, in profile order."""
    sold = set(vendors_with_commercial_path())
    src, tgt = set(sources), set(targets)
    all_systems = systems()
    mark = marks({k: s.name for k, s in all_systems.items()})
    rows = []
    for key, s in all_systems.items():
        free = tuple(s.creatable)
        if free:
            writes = ", ".join(free) if len(free) <= 2 else f"{len(free)} resource types"
            if key in sold:
                writes += ", more under licence"
            kind = "writes"
        elif key in sold:
            writes, kind = "with a licensed connector", "later"
        else:
            writes, kind = "read-only", "none"
        if key in src and key in tgt:
            use = "wired as a source and a target"
        elif key in src:
            use = "wired as a source"
        elif key in tgt:
            use = "wired as a target"
        else:
            use = "not wired here"
        rows.append(BoardRow(
            key=key, name=s.name, mark=mark[key],
            reads="bulk export" if s.supports_bulk_export else "one chart at a time",
            reads_kind="bulk" if s.supports_bulk_export else "chart",
            writes=writes, writes_kind=kind, use=use))
    return tuple(rows)


def board_summary(rows: Iterable[BoardRow]) -> dict[str, int]:
    rows = tuple(rows)
    return {
        "n": len(rows),
        "bulk": sum(1 for r in rows if r.reads_kind == "bulk"),
        "writes": sum(1 for r in rows if r.writes_kind == "writes"),
        "sells": sum(1 for r in rows if r.writes_kind == "later" or "more under licence" in r.writes),
    }


_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen", "twenty")


def count_word(n: int) -> str:
    """A small count as prose wants it ("Fifteen"); past the table, digits
    rather than a wrong word."""
    return _WORDS[n].capitalize() if 0 <= n < len(_WORDS) else str(n)
