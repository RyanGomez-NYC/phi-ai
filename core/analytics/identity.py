# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Name search over identity.patient_identity.

WHAT THIS IS FOR. A records request arrives naming a person. Everything
downstream in this platform keys on the EMR's opaque patient id, so
something has to bridge the two, and until this existed the bridge was
"go and look it up in the system you were trying to decommission".

MATCHING IS DELIBERATELY GENEROUS, AND THE RESULT SET IS DELIBERATELY
SMALL. Those pull in opposite directions and both matter. A records clerk
searching "mary smith" needs to find "Smith, Mary-Jane" and "Marie
Smith"; they must not get four hundred rows they then scroll through,
because a name search that returns a large slice of the patient list is a
patient list. So: several matching strategies, tried in order of
confidence, and a hard cap - with the count of what was suppressed, so
"too many, be more specific" is distinguishable from "no matches".

STRATEGIES, MOST CONFIDENT FIRST. Each result says which one found it, so
a caller can tell an exact match from a fuzzy one rather than treating a
ranked list as equally certain:

  exact    both names match, case- and accent-insensitively
  prefix   the family name starts with the term
  fuzzy    trigram similarity, if pg_trgm is available

pg_trgm is optional. Managed Postgres offerings differ on which
extensions they permit, and a deployment that cannot search names at all
because an extension was refused would be a poor trade - so its absence
is detected once and downgrades the search rather than failing it.

BIRTH DATE IS A FILTER, NOT A RESULT FIELD BY DEFAULT. Narrowing by date
of birth is how a clerk disambiguates two people with the same name, and
it is also the single most re-identifying thing here. It is accepted as
an input; whether it comes back is the caller's decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("phi-ai.analytics.identity")

# A name search must never be a way to page through the patient list.
MAX_RESULTS = 25

# WORD similarity, not whole-string similarity, and the threshold is
# measured rather than guessed.
#
# similarity() compares the trigram sets of two whole strings, so a short
# search term against a long stored name scores low no matter how well it
# matches part of it: measured against real data, similarity('mary jane
# smith o'brien', 'smyth') is 0.111. A 0.45 threshold on that rejects
# every realistic misspelling, which is the entire purpose of having
# fuzzy matching at all.
#
# word_similarity(term, name) instead scores the term against the
# best-matching extent within the name. The same pair scores 0.333 - and
# smyth/smith SHOULD score around a third, since they share only the
# leading and trailing trigrams. 0.30 is therefore the threshold that
# admits a one-character misspelling of a five-letter name, and it
# matches Postgres' own default for the % operator rather than inventing
# a number.
TRIGRAM_THRESHOLD = 0.30


@dataclass(frozen=True)
class Match:
    patient_reference: str
    person_id: int
    full_name: Optional[str]
    birth_date: Optional[str]
    gender: Optional[str]
    matched_by: str

    def to_dict(self, include_birth_date: bool = True) -> dict:
        row = {
            "patient_reference": self.patient_reference,
            "name": self.full_name,
            "matched_by": self.matched_by,
        }
        if include_birth_date:
            row["birth_date"] = self.birth_date
            row["gender"] = self.gender
        return row


@dataclass
class SearchResult:
    matches: list[Match]
    total_found: int
    truncated: bool
    strategies_used: list[str]
    note: Optional[str] = None

    def to_dict(self, include_birth_date: bool = True) -> dict:
        return {
            "matches": [m.to_dict(include_birth_date) for m in self.matches],
            "returned": len(self.matches),
            "total_found": self.total_found,
            "truncated": self.truncated,
            "matched_using": self.strategies_used,
            "note": self.note,
        }


def _query(conn, sql: str, params: tuple = ()) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def trigram_available(conn) -> bool:
    try:
        rows = _query(conn, "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        return bool(rows)
    except Exception:
        return False


def _normalise(value: str) -> str:
    """Fold a search term the same way the stored columns are folded.

    identity.patient_identity strips apostrophes, hyphens, periods and
    commas in its generated columns. A term that is not folded identically
    cannot match them - searching "O'Brien" against a column holding
    "obrien" fails just as surely as the reverse.
    """
    lowered = (value or "").lower()
    for character in "'-.,":
        lowered = lowered.replace(character, "")
    return " ".join(lowered.split())


def _split(term: str) -> tuple[Optional[str], Optional[str]]:
    """Split a typed name into (given, family), handling both orders.

    "mary smith" and "smith, mary" are the same query to the person
    typing them, and both turn up in real requests.
    """
    cleaned = " ".join((term or "").split())
    if not cleaned:
        return None, None
    if "," in cleaned:
        family, _, given = cleaned.partition(",")
        return given.strip() or None, family.strip() or None
    parts = cleaned.split()
    if len(parts) == 1:
        return None, parts[0]
    return " ".join(parts[:-1]), parts[-1]


_SELECT = """
    SELECT patient_reference, person_id, full_name, birth_date, gender
      FROM identity.patient_identity
"""


def search(
    conn,
    term: str,
    birth_date: Optional[str] = None,
    limit: int = MAX_RESULTS,
) -> SearchResult:
    """Find patients by name, most confident matches first."""
    term = (term or "").strip()
    if len(term) < 2:
        return SearchResult(
            matches=[], total_found=0, truncated=False, strategies_used=[],
            note="Enter at least two characters. A one-character search would return "
                 "an arbitrary slice of the patient list rather than an answer.",
        )

    given, family = _split(term)
    limit = max(1, min(int(limit), MAX_RESULTS))

    date_clause, date_params = "", []
    if birth_date:
        date_clause = " AND birth_date = %s"
        date_params = [birth_date]

    seen: dict[str, Match] = {}
    strategies: list[str] = []

    def collect(rows, strategy: str) -> None:
        if rows and strategy not in strategies:
            strategies.append(strategy)
        for row in rows:
            if row[0] in seen:
                continue
            seen[row[0]] = Match(
                patient_reference=row[0],
                person_id=int(row[1]),
                full_name=row[2],
                birth_date=str(row[3]) if row[3] else None,
                gender=row[4],
                matched_by=strategy,
            )

    lowered = _normalise(term)

    # 1. Exact - the whole name as presented, in either order.
    collect(
        _query(
            conn,
            _SELECT + f" WHERE full_name_norm = %s{date_clause} LIMIT {limit * 2}",
            tuple([lowered] + date_params),
        ),
        "exact",
    )
    if family and given:
        collect(
            _query(
                conn,
                _SELECT + f" WHERE family_name_norm = %s AND full_name_norm LIKE %s"
                f"{date_clause} LIMIT {limit * 2}",
                tuple([_normalise(family), f"%{_normalise(given)}%"] + date_params),
            ),
            "exact",
        )

    # 2. Prefix on the family name - how a clerk searches when unsure of
    #    the spelling of the rest.
    if family:
        collect(
            _query(
                conn,
                _SELECT + f" WHERE family_name_norm LIKE %s{date_clause} "
                f"ORDER BY family_name_norm LIMIT {limit * 2}",
                tuple([f"{_normalise(family)}%"] + date_params),
            ),
            "prefix",
        )

    # 3. Fuzzy, only if the extension is there and we still have room.
    if len(seen) < limit:
        if trigram_available(conn):
            collect(
                _query(
                    conn,
                    _SELECT + f" WHERE word_similarity(%s, full_name_norm) > %s{date_clause} "
                    f"ORDER BY word_similarity(%s, full_name_norm) DESC LIMIT {limit * 2}",
                    tuple([lowered, TRIGRAM_THRESHOLD] + date_params + [lowered]),
                ),
                "fuzzy",
            )
        else:
            collect(
                _query(
                    conn,
                    _SELECT + f" WHERE full_name_norm LIKE %s{date_clause} LIMIT {limit * 2}",
                    tuple([f"%{lowered}%"] + date_params),
                ),
                "contains",
            )

    ordering = {"exact": 0, "prefix": 1, "fuzzy": 2, "contains": 3}
    ranked = sorted(seen.values(), key=lambda m: (ordering.get(m.matched_by, 9), m.full_name or ""))

    total = len(ranked)
    truncated = total > limit
    note = None
    if truncated:
        note = (
            f"{total} patients matched and the first {limit} are shown. Narrow the "
            "search with a date of birth or a fuller name - a name search is not a way "
            "to browse the patient list."
        )
    elif not ranked:
        note = (
            "No patient matched. The name is searched as the source EMR recorded it, so "
            "check spelling, try the family name alone, or try a maiden name. A patient "
            "ingested before the identity index was populated will not appear until the "
            "OMOP layer is re-run."
        )

    return SearchResult(
        matches=ranked[:limit],
        total_found=total,
        truncated=truncated,
        strategies_used=strategies,
        note=note,
    )
# Made by Ryan Gomez & Co. Inc.
