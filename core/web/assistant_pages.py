# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Where in the interface a question was asked from, and where to go back to.

TWO SMALL ALLOWLISTS, BOTH DELIBERATE.

**What the model is told.** The assistant answers better when it knows
the user is standing on the retention page rather than the audit browser
- "why is this empty?" is unanswerable otherwise. What it must never
learn is WHICH patient or WHICH resource, and the difference between
those two is a URL: /retention carries nothing, /smart/patient/eAB12cd3
carries an identifier in the path.

So the model is never told a URL. It is told a phrase from the table
below, selected by a key the route itself supplies. There is no code path
that turns a request path into text for the model, which means no
future route can accidentally leak its parameters into a prompt by
existing. The phrases are constants in this file; nothing user-supplied
reaches them.

**Where "back" goes.** The drawer carries a return path so answering a
question does not lose the user's place. That value DOES come from the
request, which makes it an open-redirect and an HTML-injection surface if
taken at face value, so it is validated against a prefix allowlist and a
character class rather than trusted. A value that does not validate is
dropped and the user gets no back link - a missing convenience, not a
redirect to somewhere else's login page.
"""

from __future__ import annotations

import re
from typing import Optional

# key -> the phrase given to the model. Written as a description of what
# the user is looking at, because that is what makes a follow-up question
# make sense.
PAGE_CONTEXTS: dict[str, str] = {
    "dashboard": "the platform overview page, which shows holdings totals and the audit chain verdict",
    "patients": "the patient search page, which searches the index by the source EMR's opaque patient identifier",
    "patient": "a patient's record list (the assistant cannot see whose, or any of the records)",
    "resource": "the detail view of a single stored resource (the assistant cannot see its contents)",
    "documents": "the document ingestion page, where scanned records are uploaded and OCR'd",
    "roi": "the release-of-information page, where disclosure requests are raised and fulfilled",
    "audit": "the audit trail browser",
    "retention": "the retention schedule page, listing resources approaching their retain-until date",
    "reports": "the reports page",
    "assistant": "the assistant's own page",
}

# Paths the back link may point at. Prefixes rather than exact paths so a
# patient record or a filtered audit view still returns somewhere useful.
_RETURN_PREFIXES = (
    "/patients",
    "/documents",
    "/roi",
    "/audit",
    "/retention",
    "/reports",
    "/smart/patient/",
)

# No scheme, no host, no backslash, no percent-encoding to unpick.
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9/_.=&?-]{0,180}$")


def describe(page_key: Optional[str]) -> Optional[str]:
    """The phrase for a page key, or None for an unknown one.

    Unknown keys return None rather than the key itself. A route added
    later without a table entry produces an assistant that simply does
    not know where the user is - which is the previous behaviour, not a
    leak.
    """
    if not page_key:
        return None
    return PAGE_CONTEXTS.get(page_key.strip().lower())


def safe_return_path(raw: Optional[str]) -> Optional[str]:
    """A same-origin path safe to use as an href, or None.

    Rejects anything that is not a plain absolute path on this site.
    Protocol-relative URLs (`//evil.example`) are the specific case worth
    naming: they pass a naive "starts with /" check and navigate to
    another origin.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    if not _SAFE_PATH.match(candidate):
        return None
    if candidate == "/":
        return candidate
    if not any(candidate.startswith(prefix) for prefix in _RETURN_PREFIXES):
        return None
    return candidate


def back_label(path: Optional[str]) -> str:
    """A human name for a return path, for the back link's text."""
    if not path or path == "/":
        return "the overview"
    for prefix, label in (
        ("/patients", "patient search"),
        ("/smart/patient/", "the patient record"),
        ("/documents", "document ingestion"),
        ("/roi", "release of information"),
        ("/audit", "the audit trail"),
        ("/retention", "the retention schedule"),
        ("/reports", "reports"),
    ):
        if path.startswith(prefix):
            return label
    return "where you were"
# Made by Ryan Gomez & Co. Inc.
