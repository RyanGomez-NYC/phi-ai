# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Deterministic surrogate keys for the OMOP CDM analytics layer
(core/db/omop_schema.sql).

Every cdm.* primary key (person_id, visit_occurrence_id, etc.) is a
BIGINT the ETL process itself assigns - matching standard OMOP ETL
practice, not a Postgres-assigned BIGSERIAL the way
stored_resources.id is (core/db/schema.sql). This module computes
those IDs deterministically from a namespaced natural key, rather than
looking one up or handing out the next value from a sequence:

  - The same source FHIR resource always produces the same OMOP row id,
    computed independently by any ETL run, with no database round trip
    and no dependency on run order. A later re-ETL of the same resource
    (e.g. after a correction) lands on the exact same id, which is what
    lets the write path in core/db/omop_etl.py treat that case as an
    UPDATE against the existing row rather than a duplicate INSERT.
  - No sequence, and so no separate GRANT USAGE ON SEQUENCE needed
    beyond what core/db/omop_bootstrap_aws.sql already grants -
    omop_etl's narrow, column-scoped SELECT on person/visit_occurrence
    (see that file) remains available for verification/debugging, but
    nothing in the write path actually depends on it.
  - No coordination needed between concurrent ETL processes - two
    workers processing the same patient's records concurrently compute
    the identical person_id independently, rather than racing to
    reserve a sequence value or requiring a lock.

The tradeoff, stated plainly: this is a one-way hash, not a reversible
mapping - you cannot recover a person_source_value from a person_id
without a lookup. That's an acceptable property here, not a gap: the
whole point of every event table also carrying source_storage_key
(core/db/omop_schema.sql's own non-standard extension) is that the
actual source resource is always one storage lookup away, on the same
key already used everywhere else in this project.

FOUND AND FIXED (2026-08-17 audit, H7f): the id is now namespaced by
source_system as well as by FHIR resource type, defaulting to "epic" -
matching core/fhir/emr_profiles.py's own get_profile(vendor_key: str =
"epic") default exactly. Before this fix, deterministic_id() folded in
only `namespace` (the resource type) and `source_value` (the FHIR
resource's own opaque id) - with no EMR/tenant qualifier at all. Epic's
own FHIR ids are only guaranteed unique WITHIN a single Epic instance
(per Epic's own FHIR documentation); this project's stated mission
targets the top five US EMRs plural (see the project's own README/
mission statement), and a second EMR vendor - or even a second Epic
tenant, in a multi-tenant deployment - reusing the same raw id string
for an unrelated Patient would previously collide onto the exact same
person_id, silently merging two different people's clinical data under
one surrogate key. That is a materially worse failure mode than the
already-documented 63-bit hash-collision risk below: it is not
vanishingly rare, it is the expected outcome of two independent EMR id
sequences that were never coordinated with each other. Folding
source_system into the hash input closes this for good, at zero cost to
Epic-only deployments (default "epic" reproduces the exact prior id
space for anyone not yet running a second EMR/tenant).

ALSO CORRECTED, previously wrong in this module's own docstring below:
the collision-handling text claimed every write path in
core/db/omop_etl.py "checks source_storage_key before treating an
existing row as a match" - it does not, and never did. The
_execute_upsert() write pattern (core/db/omop_etl.py) matches an
existing row purely by primary key equality (UPDATE ... WHERE
{pk_column} = %s) on a duplicate-key INSERT; it has no code path that
reads or compares source_storage_key at all before performing that
UPDATE. A genuine 63-bit collision between two unrelated resources
would therefore silently overwrite one resource's OMOP row with the
other's data, not surface as a detected mismatch the way the old text
implied. This is not fixed by this change - a full fix would mean
teaching _execute_upsert() to verify source_storage_key matches before
committing an UPDATE, and deciding what should happen when it doesn't
(reject the write? mint a fallback id?), which is its own design
decision, out of scope for this pass. Stated here plainly rather than
left as a false claim: the collision risk is real, non-zero, and
currently undetected in the write path, not merely "unhandled but
would be caught downstream."
"""

from __future__ import annotations

import hashlib

# Postgres BIGINT is a signed 64-bit integer. Masked to the positive
# half (63 bits) below - OMOP tooling and the reference OHDSI ETL
# implementations generally assume a non-negative surrogate key, even
# though the column type itself would accept a negative value.
_POSITIVE_BIGINT_MASK = 0x7FFFFFFFFFFFFFFF

# Matches core/fhir/emr_profiles.py's get_profile(vendor_key: str =
# "epic") default exactly - see this module's own docstring (H7f) for
# why this default exists and what it preserves for single-EMR
# deployments.
_DEFAULT_SOURCE_SYSTEM = "epic"


def deterministic_id(namespace: str, source_value: str, source_system: str = _DEFAULT_SOURCE_SYSTEM) -> int:
    """
    Derives a stable, positive BIGINT from a namespaced natural key,
    e.g. deterministic_id("Patient", "eAB12cd3") ==
    deterministic_id("Patient", "eAB12cd3", source_system="epic").

    `source_system` should be the EMR vendor key (matching
    core/fhir/emr_profiles.py's own vendor_key values, e.g. "epic") -
    or, in a multi-tenant deployment of the same EMR vendor, a stable
    per-tenant qualifier. Defaults to "epic", the only EMR this project
    currently ships a profile for, so existing callers are unaffected.
    See this module's own docstring (H7f) for why this parameter
    exists: without it, two different EMR vendors' (or tenants')
    independently-issued raw FHIR ids can collide onto the same
    surrogate key.

    `namespace` should be the FHIR resource type or an equivalent
    stable qualifier (e.g. "Patient", "Encounter") - included so that,
    say, a Patient and an Encounter that happened to share a raw id
    string do not collide onto the same surrogate key. `source_value`
    should be the FHIR resource's own `id` field - the same opaque,
    EMR-internal identifier already used to construct that resource's
    storage key (fhir/{ResourceType}/{id}.json) elsewhere in this
    project.

    Collisions are theoretically possible (a 63-bit space is large but
    finite) - not handled specially here. See this module's own
    docstring (H7f correction) for the current, honest state of what
    happens on a collision: core/db/omop_etl.py's write path does NOT
    check source_storage_key before an UPDATE, so a genuine collision
    would silently overwrite one resource's row with another's data,
    not surface as a detected mismatch.
    """
    if not namespace:
        raise ValueError("namespace must be a non-empty string")
    if not source_value:
        raise ValueError("source_value must be a non-empty string")
    if not source_system:
        raise ValueError("source_system must be a non-empty string")

    digest = hashlib.sha256(f"{source_system}:{namespace}:{source_value}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") & _POSITIVE_BIGINT_MASK
# Made by Ryan Gomez & Co. Inc.
