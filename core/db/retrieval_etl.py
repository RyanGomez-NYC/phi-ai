# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Building the clinical retrieval index from the encrypted object store.

    python -m core.db.retrieval_etl                    # general store
    python -m core.db.retrieval_etl --include-psychotherapy

DERIVED AND REBUILDABLE, LIKE EVERY OTHER INDEX HERE. The object store
is the unconditional system of record; this ETL reads it, extracts each
resource's searchable prose (core/db/retrieval_text.py - the extraction
rules live there, and only there), and writes retrieval.clinical_text.
Dropping the schema and re-running reproduces it exactly, so nothing
downstream may ever treat a retrieval row as authoritative.

IDEMPOTENT BY DELETE-THEN-INSERT PER STORAGE KEY, inside one
transaction per object. A re-run after a re-ingested (corrected)
resource replaces that object's rows wholesale rather than merging - a
bundle that shrank on correction must not leave orphan rows from its
longer past self, which per-row upsert would. This is why the ETL role
holds DELETE (retrieval_bootstrap_<cloud>.sql).

UNCHANGED OBJECTS ARE SKIPPED by comparing the store's content digest
against the digest recorded when the rows were written, so routine
re-runs after an incremental ingest cost one metadata read per object
rather than one decrypt. `--rebuild` ignores the digests and re-extracts
everything - the right lever after changing extraction rules.

PSYCHOTHERAPY NOTES ARE NEVER INDEXED BY ACCIDENT. The general run
touches only the general store. `--include-psychotherapy` additionally
indexes the psychotherapy bucket - its own storage client, its own KMS
key (core/storage/factory.py's build_psychotherapy_storage, which
refuses to fall back to the general store) - into
retrieval.psychotherapy_text, a table the general search role cannot
read (see retrieval_schema.sql's header). The flag is explicit on every
run rather than remembered in configuration: indexing the most
restricted record class in the system should never be a side effect of
a routine refresh.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Iterable, Optional

from core.db.retrieval_text import resource_row

log = logging.getLogger("phi-ai.retrieval-etl")

CLINICAL_TABLE = "retrieval.clinical_text"
PSYCHOTHERAPY_TABLE = "retrieval.psychotherapy_text"

# The two spellings below are the ONLY table names this module will
# write. Interpolating a caller-supplied name into SQL is how an
# injection is born, so table is validated against this set everywhere
# it is interpolated.
_TABLES = frozenset({CLINICAL_TABLE, PSYCHOTHERAPY_TABLE})


def _assert_known_table(table: str) -> None:
    if table not in _TABLES:
        raise ValueError(f"unknown retrieval table {table!r}")


def stored_digest(conn: Any, table: str, storage_key: str) -> Optional[str]:
    """The source digest recorded when this object was last indexed.

    Rides in the rows themselves (every row of one object carries the
    same digest) rather than a separate state table, so the skip logic
    can never disagree with what is actually indexed.
    """
    _assert_known_table(table)
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT source_digest FROM {table} WHERE storage_key = %s LIMIT 1",
            (storage_key,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def index_object(
    conn: Any,
    table: str,
    storage_key: str,
    resources: Iterable[dict],
    source_digest: Optional[str] = None,
) -> int:
    """(Re)index one stored object: delete its rows, insert the current
    extraction. Returns how many rows were written. Commits."""
    _assert_known_table(table)
    rows = []
    for i, resource in enumerate(resources):
        row = resource_row(resource, storage_key, resource_index=i)
        if row is not None:
            rows.append(row)

    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {table} WHERE storage_key = %s", (storage_key,))
        for row in rows:
            cur.execute(
                f"INSERT INTO {table} "
                "(storage_key, resource_index, patient_reference, resource_type, "
                " resource_id, clinical_date, content, source_digest) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row["storage_key"], row["resource_index"],
                    row["patient_reference"], row["resource_type"],
                    row["resource_id"], row["clinical_date"], row["content"],
                    source_digest,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return len(rows)


def _resources_of(storage, encryptor, storage_key: str) -> list[dict]:
    """Decrypt one object into its resources, either layout.

    The nonce-prefix convention matches core/fhir/client.py and
    core/web/data.py exactly; integrity is verified FIRST, and a failure
    raises rather than indexes - text extracted from tampered bytes
    would launder the tampering into search results.
    """
    import json

    if not storage.verify_integrity(storage_key):
        raise ValueError(
            f"Integrity check failed for {storage_key} - not indexing it. "
            "Escalate per runbooks/RUNBOOK_INCIDENT_RESPONSE.md."
        )
    raw = storage.get_object(storage_key)
    meta = storage.get_metadata(storage_key)
    plaintext = encryptor.decrypt(raw[12:], raw[:12], meta.wrapped_dek_b64)
    if not storage_key.endswith(".ndjson"):
        return [json.loads(plaintext)]
    from core.storage.layout import parse_bundle

    return list(parse_bundle(plaintext))


def run(
    storage,
    encryptor,
    conn: Any,
    *,
    table: str = CLINICAL_TABLE,
    prefix: str = "fhir/",
    rebuild: bool = False,
) -> tuple[int, int, int]:
    """Index every object under `prefix`. Returns (objects_indexed,
    objects_skipped_unchanged, rows_written)."""
    _assert_known_table(table)
    indexed = skipped = rows_total = 0
    for storage_key in storage.iter_keys(prefix=prefix):
        meta = storage.get_metadata(storage_key)
        digest = getattr(meta, "sha256_hex", None)
        if not rebuild and digest and stored_digest(conn, table, storage_key) == digest:
            skipped += 1
            continue
        resources = _resources_of(storage, encryptor, storage_key)
        rows_total += index_object(conn, table, storage_key, resources, source_digest=digest)
        indexed += 1
        if indexed % 500 == 0:
            log.info("indexed %d objects so far (%d rows)", indexed, rows_total)
    return indexed, skipped, rows_total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rebuild", action="store_true",
                        help="re-extract every object, ignoring recorded digests")
    parser.add_argument("--include-psychotherapy", action="store_true",
                        help="ALSO index the psychotherapy store into its separate, "
                             "separately-granted table - see the module docstring")
    parser.add_argument("--prefix", default="fhir/",
                        help="general-store key prefix to index (default fhir/)")
    args = parser.parse_args()

    from core.config.settings import Settings
    from core.crypto.envelope import EnvelopeEncryptor
    from core.db.connection import connect
    from core.storage.factory import build_kms, build_storage

    settings = Settings.from_env()
    if not settings.retrieval_configured():
        print(
            "PHI_AI_RETRIEVAL_ETL_USERNAME is not set (or no database target is "
            "configured), so there is no retrieval index to write. See "
            "core/db/retrieval_schema.sql's header and runbooks/RUNBOOK_AI_ASSISTANT.md.",
            file=sys.stderr,
        )
        return 2

    conn = connect(settings, settings.retrieval_etl_username)
    try:
        storage = build_storage(settings)
        encryptor = EnvelopeEncryptor(kms=build_kms(settings))
        indexed, skipped, rows = run(
            storage, encryptor, conn,
            table=CLINICAL_TABLE, prefix=args.prefix, rebuild=args.rebuild,
        )
        print(f"clinical: {indexed} objects indexed, {skipped} unchanged, {rows} rows")

        if args.include_psychotherapy:
            from core.storage.factory import build_psychotherapy_storage

            psych_storage = build_psychotherapy_storage(settings)
            psych_encryptor = EnvelopeEncryptor(
                kms=build_kms(settings, key_id=settings.psychotherapy_kms_key_id)
            )
            indexed, skipped, rows = run(
                psych_storage, psych_encryptor, conn,
                table=PSYCHOTHERAPY_TABLE, prefix="", rebuild=args.rebuild,
            )
            print(f"psychotherapy: {indexed} objects indexed, {skipped} unchanged, {rows} rows")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
