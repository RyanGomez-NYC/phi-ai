# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Extracting the searchable prose of a FHIR resource, for the clinical
retrieval index (core/db/retrieval_schema.sql).

PURE FUNCTIONS, NO I/O. Everything here takes a decrypted resource dict
and returns text, so the extraction rules - which are the answer to
"what exactly does the retrieval index hold?" - are testable without a
database, a bucket or a key, and auditable by reading one file. The ETL
(core/db/retrieval_etl.py) owns fetching and writing.

WHAT IS EXTRACTED, AND WHY THESE FIELDS. The index exists so a
researcher can find records by what their text SAYS - "insulin pump
failure", "post-surgical infection" - so extraction targets the fields
where prose lives across the resource types this platform ingests
(core/fhir/emr_profiles.py):

  - the narrative (text.div), with its HTML stripped
  - human-readable codings: any `text` or `display` under code-bearing
    fields (code, category, reasonCode, medication, vaccineCode, ...)
  - clinical annotations: note[].text - where dictated prose actually
    lands in Condition, Observation, MedicationRequest, Procedure
  - document metadata: title, description, conclusion
  - primitive string values whose very name promises prose
    (valueString), plus DocumentReference attachment titles

WHAT IS DELIBERATELY NOT EXTRACTED:

  - identifiers, references, URLs, ids: the index is searched by
    clinical language, not by key - keys live in stored_resources, and
    putting them here would just widen what a stolen copy leaks
  - names, addresses, telecom: name search is the identity index's job
    (core/db/identity_schema.sql), behind its own role, and this table
    must not become a second, un-permissioned copy of it. Patient
    resources contribute only their narrative-free structural fields,
    which is to say: effectively nothing.
  - base64 payloads (attachment.data): OCR output for scanned documents
    is already ingested as its own searchable resource content; raw
    base64 in a tsvector is noise at best

Every rule above is a boundary someone may need to verify - keep this
docstring true when changing any of them, and keep the corresponding
test in tests/test_retrieval.py pointed at the rule, not the code.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from core.fhir.clinical_dates import clinical_date

# Fields whose string value is prose worth indexing, wherever they
# appear in the tree. `display` and `text` pull the human-readable side
# of every coding; the rest are the documented prose carriers.
_PROSE_KEYS = frozenset({
    "display", "text", "title", "description", "conclusion",
    "valueString", "comment", "patientInstruction",
})

# Keys never descended into: identifier-ish, reference-ish, binary-ish.
# `contained` is skipped because contained resources are indexed via
# their parent's prose keys only - descending would also pull their
# identifiers' `text` fields out of context.
_SKIP_KEYS = frozenset({
    "identifier", "reference", "url", "system", "id", "meta",
    "data", "hash", "telecom", "address", "name", "photo",
})

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# One row's content is capped so a single enormous narrative cannot
# dominate the table or a query plan. Postgres's own tsvector limit is
# the hard ceiling (1MB of lexeme positions); this is far below it.
MAX_CONTENT_CHARS = 20_000


def _strip_html(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _walk(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SKIP_KEYS:
                continue
            if key == "div" and isinstance(value, str):
                out.append(_strip_html(value))
            elif key in _PROSE_KEYS and isinstance(value, str):
                out.append(value.strip())
            else:
                _walk(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def extract_text(resource: dict) -> str:
    """The searchable prose of one resource, deduplicated in order.

    Patient resources get no special casing and still come out nearly
    empty, because everything identifying about them lives under keys
    the walk skips (name/telecom/address/identifier) - that is the
    "not a second identity index" rule holding structurally rather
    than by resource-type special case.
    """
    if not isinstance(resource, dict):
        return ""
    pieces: list[str] = []
    _walk(resource, pieces)
    seen: set[str] = set()
    unique = []
    for piece in pieces:
        if piece and piece not in seen:
            seen.add(piece)
            unique.append(piece)
    text = "\n".join(unique)
    return text[:MAX_CONTENT_CHARS]


def resource_row(
    resource: dict, storage_key: str, resource_index: int = 0
) -> Optional[dict]:
    """One retrieval.clinical_text row, or None when there is nothing to
    index - an empty content row would match no search and still leak
    the patient linkage for free."""
    content = extract_text(resource)
    if not content:
        return None

    parsed, _path = clinical_date(resource)
    parsed_date: Optional[date] = parsed.date() if parsed is not None else None

    subject = resource.get("subject") or resource.get("patient") or {}
    patient_reference = (
        subject.get("reference") if isinstance(subject, dict) else None
    )
    if not patient_reference and resource.get("resourceType") == "Patient" and resource.get("id"):
        patient_reference = f"Patient/{resource['id']}"

    return {
        "storage_key": storage_key,
        "resource_index": resource_index,
        "patient_reference": patient_reference,
        "resource_type": resource.get("resourceType") or "Unknown",
        "resource_id": resource.get("id"),
        "clinical_date": parsed_date,
        "content": content,
    }
# Made by Ryan Gomez & Co. Inc.
