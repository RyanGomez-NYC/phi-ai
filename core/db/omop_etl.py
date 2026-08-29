# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
FHIR-to-OMOP ETL: maps a FHIR resource already written to storage
into the corresponding row(s) in core/db/omop_schema.sql's cdm schema,
and writes them.

Scope matches core/db/omop_schema.sql exactly - Patient, Encounter,
Condition, Procedure, MedicationRequest, Immunization, and Observation.
Any other resourceType (including the three deliberately-deferred ones -
DocumentReference, AllergyIntolerance, ExplanationOfBenefit - see that
schema's own header) is a safe, logged no-op here, not an error: this
module should never be the reason a scheduler run fails.

WRITE PATTERN: attempt INSERT; on a duplicate-key error (either the
deterministic primary key - core/db/omop_ids.py - or the
source_storage_key UNIQUE constraint, whichever a re-ETL of the same
resource hits first), UPDATE the existing row instead. Not implemented
as INSERT ... ON CONFLICT, since evaluating a conflict target requires
SELECT (the same reasoning core/db/index.py's write_index_entry() gives
for stored_resources), and omop_etl deliberately does not hold broad
SELECT on the five clinical event tables - see
core/db/omop_bootstrap_aws.sql. The one deliberate exception is the
person find-or-create stub in _ensure_person_stub() below, which DOES
use INSERT ... ON CONFLICT (person_id) DO NOTHING - safe specifically
because person_id is one of the two columns omop_bootstrap_*.sql's
"find-or-create" SELECT grant already covers, and DO NOTHING (unlike DO
UPDATE) never needs to read any OTHER column to decide what to do.

The duplicate-key check in _execute_upsert() below routes through
core/db/pg_errors.py's is_unique_violation() - shared with
core/db/index.py - which reads the SQLSTATE each driver carries
structurally (psycopg's `.sqlstate` attribute; pg8000's error-field
dict) rather than importing a driver-specific exception class OR
substring-matching the rendered message. This module previously checked
`"23505" in str(exc)`: driver-agnostic, but any unrelated error whose
message happened to contain "23505" - this project's deterministic
BIGINT IDs appear verbatim inside FK-violation messages, and roughly
one in every 6,500 such IDs contains that digit run - was misread as a
duplicate and silently converted into the UPDATE fallback. The UPDATE
now also verifies it matched exactly one row and fails loudly
otherwise, so a misclassified error can never commit a 0-row no-op and
report success. See pg_errors.py's own docstring for the cross-driver
verification account.

2026-08-17 AUDIT, H7 - CORRECTNESS CLUSTER (implemented 2026-08-18).
Six issues found in this module beyond the grants themselves (C1,
already fixed). (c) was fixed in the same pass that produced pg_errors.py
above (the 0-row-UPDATE guard). The remaining five are fixed in this
pass, each with its own NOTE at the relevant function:

  (a) A FHIR Patient with no birthDate (legal, and common for
      restricted records) used to hit cdm.person.year_of_birth's NOT
      NULL constraint, so the person row never got created and every
      event for that patient FK-failed forever. Fixed by
      core/db/omop_schema.sql making year_of_birth nullable - see that
      file's own note on why this is safe pre-production, and
      write_person() below, which no longer needs to fabricate a value.

  (b) Every write_* function computed a person_id/visit_occurrence_id
      via deterministic_id() and referenced it BLIND - never checking
      whether that row actually existed - even though
      omop_bootstrap_*.sql's own header already grants exactly the
      narrow SELECT needed for "the find-or-create lookups needed to
      attach a new clinical event to the correct existing person/visit
      rather than minting a duplicate." Two distinct failure shapes
      resulted: an ordinary processing-order issue (an event ETL'd
      before its own Patient/Encounter, within the same run - usually
      self-heals on a later run once the referenced resource is
      processed) and a PERMANENT one (an Encounter skipped for missing
      period.start - see write_visit_occurrence's own existing skip -
      means its visit_occurrence row will NEVER exist, so any event
      that ever referenced it FK-fails on every single re-attempt,
      forever). Fixed with the find-or-create pattern the grants were
      already provisioned for: _ensure_person_stub() below creates a
      minimal, honestly-incomplete person row (OMOP's own "unmapped"
      concept_id=0 convention, all birth fields NULL, a
      "omop-stub:"-prefixed source_storage_key naming the resource that
      triggered its creation) before any event references that
      person_id, and _visit_occurrence_exists() checks a referenced
      visit before using it - setting visit_occurrence_id to NULL
      instead of a dangling reference when the visit doesn't (yet, or
      ever) exist, since that column is nullable on every event table
      but person_id is not. A later real write_person() call for the
      same patient safely overwrites the stub via the SAME
      unique-violation-then-UPDATE path this module already used for
      every other re-ETL case - no new write path, no new grant beyond
      what the bootstrap files already provisioned. Proven against live
      PostgreSQL 16 with the real omop_etl grants: the stub-create
      INSERT succeeds under the existing find-or-create SELECT grant,
      a second stub-create attempt after real data already landed is a
      true no-op (ON CONFLICT DO NOTHING never reads or compares
      column values beyond the conflict target), and the real
      write_person() UPDATE correctly overwrites a stub's placeholder
      values with real data.

  (d) _parse_fhir_date() collapses a partial FHIR date (a bare year or
      year-month, both legal precision levels for birthDate) into a
      full date by fabricating day=1 (and month=1 for a bare year) -
      correct behavior for an event's start/end date (a best-effort
      single date IS what's needed there), but wrong for birth
      demographics: write_person() then read month_of_birth/
      day_of_birth back OFF that fabricated date, silently asserting a
      day and sometimes a month the source data never actually stated -
      contradicting both OMOP's own convention (leave unknown
      components NULL) and this file's own adjacent comment, which
      already described the correct intended behavior without the code
      actually implementing it. Fixed with a dedicated
      _parse_fhir_birth_date() that tracks precision honestly instead
      of routing through _parse_fhir_date() at all - only used for
      Patient.birthDate; every other caller's use of _parse_fhir_date()
      is unchanged and correct for its own purpose.

  (e) An Observation whose value shape changes between ETL runs (e.g. a
      correction that adds or removes a numeric valueQuantity) can move
      from cdm.measurement to cdm.observation or back - which table a
      given Observation lands in is decided by this function, not by
      the schema (see write_measurement_or_observation's own docstring)
      - but the ETL only ever wrote to whichever table the CURRENT run
      maps to, leaving the row in the OTHER table from a prior run
      behind as a stale, orphaned duplicate with no cleanup path.
      Fixed by deleting any existing row for this same source_storage_key
      from the other table, in the same transaction as the write to the
      correct one - see _execute_upsert_with_cross_table_cleanup()
      below. Requires a narrow DELETE + SELECT(source_storage_key) grant
      on cdm.measurement/cdm.observation specifically (added to
      omop_bootstrap_{aws,gcp,azure}.sql in the same pass) - the one
      deliberate widening of omop_etl's access in this fix, scoped to
      exactly the two tables and exactly the column this cleanup needs,
      mirroring the same "SELECT needed for a WHERE clause" reasoning
      already established for the pk-column re-ETL UPDATE grants. omop_etl
      still cannot DELETE from person/visit_occurrence/condition_occurrence/
      procedure_occurrence/drug_exposure, and still cannot read any
      column beyond what's explicitly granted - verified live.

  (f) core/db/omop_ids.py's deterministic_id() previously took no
      EMR/tenant qualifier - only (namespace, source_value) - so a
      second EMR connection reusing the same raw resource id (a
      realistic, not hypothetical, event: EMR-internal IDs are commonly
      small sequential or MRN-derived values, not globally unique
      strings) would silently compute the IDENTICAL person_id/
      visit_occurrence_id/etc. as an existing patient from a DIFFERENT
      source system, and the existing unique-violation-then-UPDATE path
      would then overwrite one patient's real data with another's -
      not a a theoretical hash collision, but a structural certainty
      the moment a second EMR goes live, for any id that happens to
      match. That module's own docstring additionally overclaimed a
      protection that was never implemented ("every write path in
      core/db/omop_etl.py checks source_storage_key before treating an
      existing row as a match" - no write path ever did this). Fixed by
      adding a source_system parameter to deterministic_id() (default
      "epic", matching core/fhir/emr_profiles.py's own
      get_profile(vendor_key: str = "epic") default exactly) folded
      into the hash input, and correcting the docstring to state
      plainly what protection exists today (structural separation by
      source_system, nothing beyond that) rather than a check that
      isn't there. Every call site in this module still relies on the
      default - correct today, since this project is deliberately
      Epic-only (see emr_profiles.py's own header) - but MUST be
      updated to pass the actual configured profile's vendor key
      explicitly the day a second EMR connector is added, or this same
      collision risk reopens for anyone still on the default. Changing
      the hash input changes every already-computed OMOP row id - safe
      to do now, and only now, before any real deployment has stored
      data: this layer is explicitly "derived, rebuildable" (this
      file's own header) and the storage backend itself, the actual
      system of record, is completely untouched by this change. A
      deployment that already has OMOP rows from before this fix should
      simply re-run the ETL over its whole store after upgrading - see
      runbooks/RUNBOOK_OMOP_SETUP.md's own note on this, once added.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from core.db.omop_concepts import ethnicity_concept_id, gender_concept_id, race_concept_id
from core.db.omop_ids import deterministic_id
from core.db.pg_errors import is_unique_violation

log = logging.getLogger("phi-ai.omop_etl")

# ---------------------------------------------------------------------------
# Type Concept provenance values - see this module's own docstring on
# why these default to 0 (OMOP's "unmapped" convention) rather than a
# specific asserted concept_id. Unlike gender_concept_id and
# visit_concept_id (core/db/omop_concepts.py, this module below - both
# confirmed against multiple independent OHDSI sources), a *_type_concept_id
# value declaring "this record came from an EHR" was NOT verified to
# that same standard during this module's own research and is not
# asserted here with false confidence. 0 is a valid, insertable value
# (there is no CHECK constraint against it) and produces a row that is
# structurally complete and queryable, just without meaningful
# provenance classification until these are set correctly.
#
# TO FIX: once your loaded vocabulary includes the "Type Concept"
# vocabulary_id, replace these with the correct concept_id per domain
# (visit/condition/drug/procedure/measurement/observation each has its
# own convention - they are NOT interchangeable) - or look them up via
# core/db/omop_concepts.py's lookup_concept() against vocabulary_id =
# "Type Concept" if your loaded vocabulary indexes them that way.
VISIT_TYPE_CONCEPT_ID = 0
CONDITION_TYPE_CONCEPT_ID = 0
PROCEDURE_TYPE_CONCEPT_ID = 0
DRUG_TYPE_CONCEPT_ID_ORDERED = 0  # MedicationRequest - an order, not confirmed administration; see this module's own note on drug_exposure below
DRUG_TYPE_CONCEPT_ID_IMMUNIZATION = 0
MEASUREMENT_TYPE_CONCEPT_ID = 0
OBSERVATION_TYPE_CONCEPT_ID = 0

# Confirmed against multiple independent OHDSI sources (OHDSI's own CDM
# wiki documentation, several peer-reviewed OMOP process-mining papers,
# and the OHDSI Vocabulary-v5.0 GitHub issue tracker) - see this
# module's own docstring. Keyed on FHIR Encounter.class codes
# (http://terminology.hl7.org/CodeSystem/v3-ActCode).
_VISIT_CONCEPT_IDS: dict[str, int] = {
    "AMB": 9202,    # ambulatory -> Outpatient Visit
    "IMP": 9201,    # inpatient encounter -> Inpatient Visit
    "EMER": 9203,   # emergency -> Emergency Room Visit
}


def _visit_concept_id(fhir_encounter_class: Optional[str]) -> int:
    if not fhir_encounter_class:
        return 0
    return _VISIT_CONCEPT_IDS.get(fhir_encounter_class.upper(), 0)


def _parse_fhir_date(value: Optional[str]) -> Optional[date]:
    """
    FHIR date/dateTime values are ISO 8601 but with variable precision
    (a bare year, year-month, or full date/datetime are all valid) -
    this returns the DATE portion only, parsed as leniently as FHIR
    itself allows. Returns None for an absent or unparseable value
    rather than raising - a missing date on a single resource should
    not fail the whole ETL run.

    Used for clinical EVENT dates (encounter/condition/procedure/drug/
    observation) where a best-effort single date is what's actually
    needed - a partial "1958-06" reasonably approximates to "2026-06-01"
    for a start-date field. NOT used for Patient.birthDate - see
    _parse_fhir_birth_date() below for why that field needs precision
    tracked honestly instead of approximated away (2026-08-17 audit,
    H7d).
    """
    if not value:
        return None
    try:
        # datetime.fromisoformat handles "YYYY-MM-DD" and full
        # datetimes (with a timezone offset, from Python 3.11+) but not
        # a bare "YYYY" or "YYYY-MM" - those fall through to the
        # explicit checks below.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    parts = value.split("-")
    try:
        if len(parts) == 1:
            return date(int(parts[0]), 1, 1)
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError):
        pass
    log.warning("Could not parse FHIR date value %r - leaving unset rather than guessing.", value)
    return None


def _parse_fhir_birth_date(
    value: Optional[str],
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[datetime]]:
    """
    Parses FHIR Patient.birthDate into (year, month, day, birth_datetime)
    - honestly reflecting whatever precision the source value actually
    had, rather than fabricating the missing components the way
    _parse_fhir_date() reasonably does for event dates.

    FIXED (2026-08-17 audit, H7d). Previously, write_person() called
    _parse_fhir_date() and then read .month/.day back off the result -
    but _parse_fhir_date() always returns a FULL date, silently
    defaulting day to 1 (and month to 1, for a bare year) when the
    source value was only "1958" or "1958-06". That meant a patient
    whose birthDate was recorded only to the year had month_of_birth=1,
    day_of_birth=1, and a fabricated birth_datetime asserted anyway -
    contradicting both OMOP's own convention (leave the unknown
    components NULL) and this module's own prior comment ("constructing
    midnight-UTC from a partial date would assert a precision the
    source data didn't have"), which described the correct intended
    behavior without the code actually implementing it.

    - "1958"       -> (1958, None, None, None)
    - "1958-06"    -> (1958, 6, None, None)
    - "1958-06-12" -> (1958, 6, 12, datetime(1958, 6, 12))
    - absent/unparseable -> (None, None, None, None)
    """
    if not value:
        return None, None, None, None

    try:
        full = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return full.year, full.month, full.day, datetime(full.year, full.month, full.day)
    except ValueError:
        pass

    parts = value.split("-")
    try:
        if len(parts) == 1:
            return int(parts[0]), None, None, None
        if len(parts) == 2:
            return int(parts[0]), int(parts[1]), None, None
    except (ValueError, IndexError):
        pass

    log.warning("Could not parse FHIR birthDate value %r - leaving unset rather than guessing.", value)
    return None, None, None, None


def _extract_reference_id(resource: dict, field: str, expected_type: str) -> Optional[str]:
    """
    Pulls the bare resource id out of a FHIR reference field (e.g.
    resource["subject"]["reference"] == "Patient/eAB12cd3" -> "eAB12cd3"),
    checking the reference actually points at expected_type rather than
    assuming it. Returns None if the field is absent or doesn't match -
    a normal, non-error outcome for optional reference fields.
    """
    ref = resource.get(field, {}).get("reference", "")
    prefix = f"{expected_type}/"
    if not ref.startswith(prefix):
        return None
    return ref[len(prefix):]


def _ensure_person_stub(conn: Any, person_id: int, patient_ref_id: str, referencing_storage_key: str) -> None:
    """
    Find-or-create for cdm.person, in the sense omop_bootstrap_*.sql's
    own header already describes ("the find-or-create lookups needed to
    attach a new clinical event to the correct existing person... rather
    than minting a duplicate") but this module never actually
    implemented until this fix (2026-08-17 audit, H7b).

    Called before ANY event write that references a person_id -
    cdm.person_id is NOT NULL on every event table, so an event whose
    Patient hasn't been ETL'd yet (an ordinary processing-order issue,
    within a single run or across runs) would otherwise FK-fail. Unlike
    the visit_occurrence case (nullable on every referencing table, so
    simply omitted when absent - see _visit_occurrence_exists() below),
    person_id has no "just leave it out" option: every clinical event
    genuinely belongs to a specific patient.

    Uses INSERT ... ON CONFLICT (person_id) DO NOTHING - the one
    deliberate exception to this module's own "not ON CONFLICT, it
    needs SELECT" rule (see module docstring), safe specifically
    because (a) person_id is one of the two columns
    omop_bootstrap_*.sql's find-or-create SELECT grant already covers,
    and (b) DO NOTHING never needs to read any OTHER column to decide
    what to do, unlike a conditional DO UPDATE would. Proven live: this
    INSERT succeeds under the existing grant with no widening needed.

    The stub row is honestly incomplete, not a guess: gender/race/
    ethnicity concept_id are OMOP's own "unmapped" convention (0, same
    value this module already uses for every not-yet-vocabulary-mapped
    concept_id elsewhere), every birth field is NULL (valid as of the
    2026-08-17 audit's H7a schema fix), and source_storage_key is
    prefixed "omop-stub:" followed by the REFERENCING resource's own
    storage key (the Condition/Encounter/etc. that triggered this
    stub's creation, not a fabricated value) - honest provenance for
    "this stub exists because this resource referenced a Patient not
    yet seen," not a claim that this key IS the Patient's own stored
    object.

    When the real Patient resource is later ETL'd, write_person()'s own
    ordinary INSERT hits this exact row as a unique-key violation and
    falls through to its EXISTING plain UPDATE ... WHERE person_id = %s
    path (already granted, no new grant needed) - which unconditionally
    overwrites every column, so real data always wins over a stub's
    placeholders, with no special-casing required. A second stub-create
    attempt for an already-real (or already-stubbed) person_id is
    correctly a true no-op either way - proven live.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO cdm.person "
            "(person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, "
            "person_source_value, source_storage_key) "
            "VALUES (%s, 0, NULL, 0, 0, %s, %s) "
            "ON CONFLICT (person_id) DO NOTHING",
            [person_id, patient_ref_id, f"omop-stub:{referencing_storage_key}"],
        )
    finally:
        cur.close()
    conn.commit()


def _visit_occurrence_exists(conn: Any, visit_occurrence_id: Optional[int]) -> bool:
    """
    Whether a visit_occurrence row for this id already exists - used to
    decide whether an event should actually carry this
    visit_occurrence_id or fall back to NULL (2026-08-17 audit, H7b).

    Unlike person_id, no stub-creation counterpart exists here on
    purpose: visit_occurrence has its own NOT NULL date/type/concept
    columns that would require fabricating values with no source basis
    (the same "don't fabricate" principle this fix applies to birth
    dates - see H7d above) - and visit_occurrence_id IS nullable on
    every table that references it, so simply omitting the link is the
    honest choice, not a workaround. This is also what actually fixes
    the PERMANENT failure mode the audit called out: an Encounter
    skipped for missing period.start (see write_visit_occurrence's own
    existing skip) will never get a visit_occurrence row, ever - before
    this fix, every event that ever referenced it FK-failed on every
    single re-attempt, forever; now it's written once, cleanly, with no
    visit link, rather than never written at all.

    Uses the same find-or-create SELECT grant
    omop_bootstrap_*.sql already provisions for visit_occurrence
    (visit_occurrence_id, visit_source_value, person_id) - no new grant
    needed. Proven live.
    """
    if visit_occurrence_id is None:
        return False
    cur = conn.cursor()
    try:
        cur.execute("SELECT visit_occurrence_id FROM cdm.visit_occurrence WHERE visit_occurrence_id = %s", [visit_occurrence_id])
        return cur.fetchone() is not None
    finally:
        cur.close()


def _resolved_visit_occurrence_id(conn: Any, encounter_ref_id: Optional[str]) -> Optional[int]:
    """Shared by every write_* function below that can optionally link
    to an Encounter: computes the deterministic id (if a reference is
    present at all) and returns it ONLY if that visit_occurrence row
    actually exists yet - otherwise None. See _visit_occurrence_exists()
    for why NULL, not a dangling reference, is the correct fallback."""
    if not encounter_ref_id:
        return None
    candidate = deterministic_id("Encounter", encounter_ref_id)
    if _visit_occurrence_exists(conn, candidate):
        return candidate
    log.debug(
        "Encounter %s has no visit_occurrence row yet (not yet ETL'd, or was skipped for a missing "
        "period.start) - writing this event without a visit link rather than a dangling FK reference.",
        encounter_ref_id,
    )
    return None


def _execute_upsert(conn: Any, table: str, columns: list[str], values: list[Any], pk_column: str) -> str:
    """
    Attempts INSERT; on a unique-constraint violation, UPDATEs the
    existing row instead. Duplicate detection goes through
    core/db/pg_errors.py's is_unique_violation() - see this module's
    own docstring for the two bugs (driver-class coupling elsewhere,
    substring misclassification here) that replaced the previous
    message-text check.

    The UPDATE runs `WHERE {pk_column} = %s`, which requires the
    omop_etl role to hold SELECT on that pk column - Postgres requires
    SELECT privilege on every column an UPDATE's WHERE clause reads.
    Granted, column-scoped, in core/db/omop_bootstrap_{aws,gcp,azure}.sql;
    see those files' FOUND AND FIXED notes. Unconditional column-by-
    column SET, no WHERE-clause filtering beyond the pk match - this is
    what lets _ensure_person_stub()'s stub rows above be transparently
    overwritten by real data with no special-casing (see that
    function's own docstring).

    A 0-row UPDATE is raised as a real error, never committed and
    reported as "updated": with deterministic IDs, the row a duplicate
    key collided with must exist, so matching nothing means the
    duplicate classification itself was wrong.

    Cursor cleanup is explicit close() in finally, not
    `with conn.cursor()` - DB-API 2.0 does not promise cursors are
    context managers, and GCP connections here are pg8000, not psycopg
    (see core/db/connection.py).

    Returns "inserted" or "updated" - callers use this only for
    logging/counting, not control flow.
    """
    placeholders = ", ".join(["%s"] * len(columns))
    column_list = ", ".join(columns)
    insert_sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"

    try:
        cur = conn.cursor()
        try:
            cur.execute(insert_sql, values)
        finally:
            cur.close()
        conn.commit()
        return "inserted"
    except Exception as exc:
        conn.rollback()
        if not is_unique_violation(exc):
            raise

        pk_index = columns.index(pk_column)
        pk_value = values[pk_index]
        set_columns = [c for c in columns if c != pk_column]
        set_values = [v for c, v in zip(columns, values) if c != pk_column]
        set_clause = ", ".join(f"{c} = %s" for c in set_columns)
        update_sql = f"UPDATE {table} SET {set_clause} WHERE {pk_column} = %s"

        cur = conn.cursor()
        try:
            cur.execute(update_sql, set_values + [pk_value])
            updated = cur.rowcount
        finally:
            cur.close()
        if updated != 1:
            conn.rollback()
            raise RuntimeError(
                f"OMOP upsert fallback UPDATE on {table} matched {updated} row(s) for "
                f"{pk_column}={pk_value} - expected exactly 1. The duplicate-key "
                "classification that led here was likely wrong; refusing to report a "
                "no-op as success."
            )
        conn.commit()
        return "updated"


def _execute_upsert_with_cross_table_cleanup(
    conn: Any,
    table: str,
    other_table: str,
    storage_key: str,
    columns: list[str],
    values: list[Any],
    pk_column: str,
) -> str:
    """
    Only used by write_measurement_or_observation() below - see that
    function's own docstring and this module's H7e note. Removes any
    row for this same source_storage_key from `other_table` (the
    Observation-derived table this ETL is NOT writing to this time) in
    the SAME transaction as the INSERT attempt into `table`, so a value-
    type change between ETL runs never leaves a stale, orphaned
    duplicate behind in the table it no longer belongs in.

    Proven live against real Postgres with the narrow DELETE +
    SELECT(source_storage_key) grant omop_bootstrap_*.sql now adds on
    cdm.measurement/cdm.observation specifically (2026-08-17 audit,
    H7e) - and that this narrow grant does NOT extend DELETE to
    person/visit_occurrence/condition_occurrence/procedure_occurrence/
    drug_exposure, which still correctly deny it.

    Transaction shape, deliberately: DELETE-from-other-table and the
    INSERT attempt share one transaction. For an ordinary re-ETL of an
    UNCHANGED value type, the DELETE finds nothing (0 rows - the
    Observation was never in the other table) and the INSERT hits its
    OWN table's unique violation as usual; the whole transaction rolls
    back and falls through to _execute_upsert()'s normal UPDATE-by-pk
    path, which is unaffected. For the genuine value-type-switch case,
    both the DELETE (removing the stale row) and the INSERT (writing
    the new one) commit together, or neither does - a stale row is
    never left half-cleaned-up by a subsequent unrelated failure.
    """
    placeholders = ", ".join(["%s"] * len(columns))
    column_list = ", ".join(columns)
    insert_sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
    delete_sql = f"DELETE FROM {other_table} WHERE source_storage_key = %s"

    try:
        cur = conn.cursor()
        try:
            cur.execute(delete_sql, [storage_key])
            cur.execute(insert_sql, values)
        finally:
            cur.close()
        conn.commit()
        return "inserted"
    except Exception as exc:
        conn.rollback()
        if not is_unique_violation(exc):
            raise
        # Same-table re-ETL of an unchanged value type - fall through to
        # the ordinary upsert path; the (harmless, 0-row-or-not) delete
        # above was rolled back along with the failed insert.
        return _execute_upsert(conn, table, columns, values, pk_column)


# ---------------------------------------------------------------------------
# Per-resource mapping + write functions
# ---------------------------------------------------------------------------


def write_person(conn: Any, resource: dict, storage_key: str) -> str:
    """Maps and writes a FHIR Patient resource to cdm.person."""
    patient_id = resource["id"]
    person_id = deterministic_id("Patient", patient_id)

    # H7d fix: precision-honest parse - see _parse_fhir_birth_date()'s
    # own docstring for why this replaced _parse_fhir_date() here
    # specifically. H7a fix: year_of_birth may now legitimately be None
    # (core/db/omop_schema.sql no longer requires it NOT NULL) rather
    # than this function needing to fabricate one.
    year_of_birth, month_of_birth, day_of_birth, birth_datetime = _parse_fhir_birth_date(resource.get("birthDate"))

    race_id, race_source = race_concept_id(conn, resource)
    ethnicity_id, ethnicity_source = ethnicity_concept_id(conn, resource)

    gender_source = resource.get("gender")

    columns = [
        "person_id", "gender_concept_id", "year_of_birth", "month_of_birth", "day_of_birth",
        "birth_datetime", "race_concept_id", "ethnicity_concept_id", "person_source_value",
        "gender_source_value", "race_source_value", "ethnicity_source_value", "source_storage_key",
    ]
    values = [
        person_id, gender_concept_id(gender_source), year_of_birth, month_of_birth, day_of_birth,
        birth_datetime, race_id, ethnicity_id, patient_id,
        gender_source, race_source, ethnicity_source, storage_key,
    ]
    # _execute_upsert's unconditional column-by-column UPDATE is exactly
    # what lets this call transparently overwrite a stub row created by
    # _ensure_person_stub() (H7b) with real data - no special-casing.
    return _execute_upsert(conn, "cdm.person", columns, values, pk_column="person_id")


def write_visit_occurrence(conn: Any, resource: dict, storage_key: str) -> Optional[str]:
    """Maps and writes a FHIR Encounter resource to cdm.visit_occurrence."""
    encounter_id = resource["id"]
    patient_ref_id = _extract_reference_id(resource, "subject", "Patient")
    if not patient_ref_id:
        log.warning("Encounter %s has no resolvable Patient subject reference - skipping.", encounter_id)
        return None

    period = resource.get("period", {})
    start_date = _parse_fhir_date(period.get("start"))
    end_date = _parse_fhir_date(period.get("end")) or start_date
    if not start_date:
        log.warning("Encounter %s has no usable period.start - skipping.", encounter_id)
        return None

    encounter_class = resource.get("class", {}).get("code")

    person_id = deterministic_id("Patient", patient_ref_id)
    # H7b fix: ensures the referenced Patient has at least a stub row
    # before this Encounter's NOT NULL person_id FK is written - see
    # _ensure_person_stub()'s own docstring.
    _ensure_person_stub(conn, person_id, patient_ref_id, storage_key)

    # The facility this encounter happened at. Written BEFORE the visit
    # row so the FK target exists; returns None when the Encounter names
    # no organisation, which is common and not an error.
    care_site_id = write_care_site(conn, resource, storage_key)

    columns = [
        "visit_occurrence_id", "person_id", "visit_concept_id", "visit_start_date", "visit_end_date",
        "visit_type_concept_id", "visit_source_value", "care_site_id", "source_storage_key",
    ]
    values = [
        deterministic_id("Encounter", encounter_id),
        person_id,
        _visit_concept_id(encounter_class),
        start_date,
        end_date,
        VISIT_TYPE_CONCEPT_ID,
        encounter_id,
        care_site_id,
        storage_key,
    ]
    return _execute_upsert(conn, "cdm.visit_occurrence", columns, values, pk_column="visit_occurrence_id")


def write_condition_occurrence(conn: Any, resource: dict, storage_key: str) -> Optional[str]:
    """Maps and writes a FHIR Condition resource to cdm.condition_occurrence."""
    condition_id = resource["id"]
    patient_ref_id = _extract_reference_id(resource, "subject", "Patient")
    if not patient_ref_id:
        log.warning("Condition %s has no resolvable Patient subject reference - skipping.", condition_id)
        return None

    onset = resource.get("onsetDateTime") or resource.get("recordedDate")
    start_date = _parse_fhir_date(onset)
    if not start_date:
        log.warning("Condition %s has no usable onset/recorded date - skipping.", condition_id)
        return None

    coding = (resource.get("code", {}).get("coding") or [{}])[0]
    condition_source_value_code = coding.get("code")

    person_id = deterministic_id("Patient", patient_ref_id)
    _ensure_person_stub(conn, person_id, patient_ref_id, storage_key)  # H7b

    encounter_ref_id = _extract_reference_id(resource, "encounter", "Encounter")
    visit_occurrence_id = _resolved_visit_occurrence_id(conn, encounter_ref_id)  # H7b

    columns = [
        "condition_occurrence_id", "person_id", "condition_concept_id", "condition_start_date",
        "condition_type_concept_id", "visit_occurrence_id", "condition_source_value", "source_storage_key",
    ]
    values = [
        deterministic_id("Condition", condition_id),
        person_id,
        0,  # condition_concept_id - resolved via lookup_concept() once vocabulary is loaded; see this module's TODO section
        start_date,
        CONDITION_TYPE_CONCEPT_ID,
        visit_occurrence_id,
        condition_source_value_code,
        storage_key,
    ]
    return _execute_upsert(conn, "cdm.condition_occurrence", columns, values, pk_column="condition_occurrence_id")


def write_procedure_occurrence(conn: Any, resource: dict, storage_key: str) -> Optional[str]:
    """Maps and writes a FHIR Procedure resource to cdm.procedure_occurrence."""
    procedure_id = resource["id"]
    patient_ref_id = _extract_reference_id(resource, "subject", "Patient")
    if not patient_ref_id:
        log.warning("Procedure %s has no resolvable Patient subject reference - skipping.", procedure_id)
        return None

    performed = resource.get("performedDateTime") or resource.get("performedPeriod", {}).get("start")
    procedure_date = _parse_fhir_date(performed)
    if not procedure_date:
        log.warning("Procedure %s has no usable performed date - skipping.", procedure_id)
        return None

    coding = (resource.get("code", {}).get("coding") or [{}])[0]
    procedure_source_value_code = coding.get("code")

    person_id = deterministic_id("Patient", patient_ref_id)
    _ensure_person_stub(conn, person_id, patient_ref_id, storage_key)  # H7b

    encounter_ref_id = _extract_reference_id(resource, "encounter", "Encounter")
    visit_occurrence_id = _resolved_visit_occurrence_id(conn, encounter_ref_id)  # H7b

    columns = [
        "procedure_occurrence_id", "person_id", "procedure_concept_id", "procedure_date",
        "procedure_type_concept_id", "visit_occurrence_id", "procedure_source_value", "source_storage_key",
    ]
    values = [
        deterministic_id("Procedure", procedure_id),
        person_id,
        0,  # procedure_concept_id - resolved via lookup_concept() once vocabulary is loaded
        procedure_date,
        PROCEDURE_TYPE_CONCEPT_ID,
        visit_occurrence_id,
        procedure_source_value_code,
        storage_key,
    ]
    return _execute_upsert(conn, "cdm.procedure_occurrence", columns, values, pk_column="procedure_occurrence_id")


def write_drug_exposure(conn: Any, resource: dict, storage_key: str) -> Optional[str]:
    """
    Maps and writes a FHIR MedicationRequest OR Immunization resource to
    cdm.drug_exposure - both are in-scope for OMOP's drug domain (see
    core/db/omop_schema.sql's own note that vaccines are explicitly
    included). drug_type_concept_id differs by which resource this is:
    a MedicationRequest is an ORDER, not confirmed administration - see
    this module's DRUG_TYPE_CONCEPT_ID_ORDERED vs
    DRUG_TYPE_CONCEPT_ID_IMMUNIZATION constants above, both currently 0
    pending vocabulary-backed values, but kept as SEPARATE constants
    deliberately so that distinction is preserved once they're set,
    rather than collapsed into one shared value.
    """
    resource_type = resource["resourceType"]
    resource_id = resource["id"]

    if resource_type == "MedicationRequest":
        patient_ref_id = _extract_reference_id(resource, "subject", "Patient")
        start_value = resource.get("authoredOn")
        coding = (resource.get("medicationCodeableConcept", {}).get("coding") or [{}])[0]
        drug_type_concept_id = DRUG_TYPE_CONCEPT_ID_ORDERED
    elif resource_type == "Immunization":
        patient_ref_id = _extract_reference_id(resource, "patient", "Patient")
        start_value = resource.get("occurrenceDateTime")
        coding = (resource.get("vaccineCode", {}).get("coding") or [{}])[0]
        drug_type_concept_id = DRUG_TYPE_CONCEPT_ID_IMMUNIZATION
    else:
        log.warning("write_drug_exposure() called with unsupported resourceType %r - skipping.", resource_type)
        return None

    if not patient_ref_id:
        log.warning("%s %s has no resolvable Patient reference - skipping.", resource_type, resource_id)
        return None

    start_date = _parse_fhir_date(start_value)
    if not start_date:
        log.warning("%s %s has no usable date - skipping.", resource_type, resource_id)
        return None

    drug_source_value_code = coding.get("code")

    person_id = deterministic_id("Patient", patient_ref_id)
    _ensure_person_stub(conn, person_id, patient_ref_id, storage_key)  # H7b

    columns = [
        "drug_exposure_id", "person_id", "drug_concept_id", "drug_exposure_start_date",
        "drug_exposure_end_date", "drug_type_concept_id", "drug_source_value", "source_storage_key",
    ]
    values = [
        deterministic_id(resource_type, resource_id),
        person_id,
        0,  # drug_concept_id - resolved via lookup_concept() once vocabulary is loaded
        start_date,
        start_date,  # end_date defaults to start_date absent better information - a single order/administration event, not a course of therapy with a known end
        drug_type_concept_id,
        drug_source_value_code,
        storage_key,
    ]
    return _execute_upsert(conn, "cdm.drug_exposure", columns, values, pk_column="drug_exposure_id")


def write_measurement_or_observation(conn: Any, resource: dict, storage_key: str) -> Optional[str]:
    """
    Maps and writes a FHIR Observation resource to EITHER
    cdm.measurement (when a numeric valueQuantity is present) or
    cdm.observation (everything else) - see core/db/omop_schema.sql's
    own note on this split. Which table a given Observation lands in is
    decided here, not by the schema.

    H7e fix: since the SAME observation_id can legitimately land in
    EITHER table depending on the CURRENT run's value shape, a
    correction that changes that shape between runs is routed through
    _execute_upsert_with_cross_table_cleanup() (not the plain
    _execute_upsert() every other write_* function uses) so the row
    left behind in the table this run does NOT write to gets removed,
    rather than becoming a permanent, orphaned stale duplicate. See that
    function's own docstring for the transaction shape and the narrow
    DELETE grant this requires.
    """
    observation_id = resource["id"]
    patient_ref_id = _extract_reference_id(resource, "subject", "Patient")
    if not patient_ref_id:
        log.warning("Observation %s has no resolvable Patient subject reference - skipping.", observation_id)
        return None

    effective = resource.get("effectiveDateTime") or resource.get("effectivePeriod", {}).get("start")
    obs_date = _parse_fhir_date(effective)
    if not obs_date:
        log.warning("Observation %s has no usable effective date - skipping.", observation_id)
        return None

    coding = (resource.get("code", {}).get("coding") or [{}])[0]
    source_value_code = coding.get("code")

    person_id = deterministic_id("Patient", patient_ref_id)
    _ensure_person_stub(conn, person_id, patient_ref_id, storage_key)  # H7b

    encounter_ref_id = _extract_reference_id(resource, "encounter", "Encounter")
    visit_occurrence_id = _resolved_visit_occurrence_id(conn, encounter_ref_id)  # H7b

    value_quantity = resource.get("valueQuantity")
    pk_value = deterministic_id("Observation", observation_id)

    if value_quantity and "value" in value_quantity:
        columns = [
            "measurement_id", "person_id", "measurement_concept_id", "measurement_date",
            "measurement_type_concept_id", "value_as_number", "unit_source_value",
            "visit_occurrence_id", "measurement_source_value", "source_storage_key",
        ]
        values = [
            pk_value,
            person_id,
            0,  # measurement_concept_id - resolved via lookup_concept() once vocabulary is loaded
            obs_date,
            MEASUREMENT_TYPE_CONCEPT_ID,
            value_quantity.get("value"),
            value_quantity.get("unit"),
            visit_occurrence_id,
            source_value_code,
            storage_key,
        ]
        return _execute_upsert_with_cross_table_cleanup(
            conn, "cdm.measurement", "cdm.observation", storage_key, columns, values, pk_column="measurement_id"
        )

    value_string = resource.get("valueString") or resource.get("valueCodeableConcept", {}).get("text")
    columns = [
        "observation_id", "person_id", "observation_concept_id", "observation_date",
        "observation_type_concept_id", "value_as_string",
        "visit_occurrence_id", "observation_source_value", "source_storage_key",
    ]
    values = [
        pk_value,
        person_id,
        0,  # observation_concept_id - resolved via lookup_concept() once vocabulary is loaded
        obs_date,
        OBSERVATION_TYPE_CONCEPT_ID,
        value_string,
        visit_occurrence_id,
        source_value_code,
        storage_key,
    ]
    return _execute_upsert_with_cross_table_cleanup(
        conn, "cdm.observation", "cdm.measurement", storage_key, columns, values, pk_column="observation_id"
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_DEFERRED_RESOURCE_TYPES = frozenset({"DocumentReference", "AllergyIntolerance", "ExplanationOfBenefit"})


def etl_resource(conn: Any, resource: dict, storage_key: str) -> Optional[str]:
    """
    Routes a single stored FHIR resource to its OMOP mapping function,
    if one exists. Returns None (a safe, expected no-op, not an error)
    for the three deliberately-deferred resource types
    (core/db/omop_schema.sql's own header) and for any resource type
    this project doesn't ingest at all - the same "unrecognized input
    is not automatically an error" posture core/db/index.py's
    extract_patient_reference() already takes.
    """
    resource_type = resource.get("resourceType")

    if resource_type in _DEFERRED_RESOURCE_TYPES:
        log.debug("Skipping OMOP mapping for %s (deliberately deferred - see omop_schema.sql).", resource_type)
        return None

    if resource_type == "Patient":
        result = write_person(conn, resource, storage_key)
        # The identity index is a SEPARATE, separately-enabled store (see
        # core/db/identity_schema.sql) and must never take the OMOP write
        # down with it - a deployment that has not installed the identity
        # schema has to ETL normally, not fail on every Patient.
        #
        # A SAVEPOINT, not a try/except. Catching the exception in Python
        # is not enough and the difference is not subtle: in Postgres a
        # failed statement aborts the entire transaction, so a swallowed
        # "relation identity.patient_identity does not exist" leaves the
        # connection in an aborted state and EVERY subsequent write in the
        # batch fails with "current transaction is aborted" - turning an
        # optional feature being absent into total ETL failure. Rolling
        # back to a savepoint undoes only the failed statement.
        _write_identity_if_available(conn, resource, storage_key)
        return result
    if resource_type == "Encounter":
        return write_visit_occurrence(conn, resource, storage_key)
    if resource_type == "Condition":
        return write_condition_occurrence(conn, resource, storage_key)
    if resource_type == "Procedure":
        return write_procedure_occurrence(conn, resource, storage_key)
    if resource_type in ("MedicationRequest", "Immunization"):
        return write_drug_exposure(conn, resource, storage_key)
    if resource_type == "Observation":
        return write_measurement_or_observation(conn, resource, storage_key)

    log.debug("No OMOP mapping for resourceType %r - skipping.", resource_type)
    return None


# ---------------------------------------------------------------------------
# Facility (cdm.care_site) and name search (identity.patient_identity).
#
# Both fill gaps that made whole categories of question unanswerable: the
# facility dimension existed only as a dangling FK column, and names lived
# nowhere outside encrypted objects. See core/db/omop_schema.sql's
# care_site header and core/db/identity_schema.sql's own, longer, warning.
# ---------------------------------------------------------------------------


def _care_site_from_encounter(resource: dict) -> tuple[Optional[str], Optional[str]]:
    """(organisation id, display name) for a FHIR Encounter, or (None, None).

    Reads Encounter.serviceProvider first - the organisation responsible
    for the encounter, which is what "which facility" almost always
    means - and falls back to the first Encounter.location. Both carry a
    reference and, usually, a human-readable display string that Epic
    populates.

    This project does not ingest Organization resources (see
    core/fhir/emr_profiles.py), so the display text IS the name; there is
    nothing to join to. That is a real limitation and it is recorded in
    the schema rather than hidden here.
    """
    provider = resource.get("serviceProvider") or {}
    reference = provider.get("reference") or ""
    display = provider.get("display")

    if not reference:
        for entry in resource.get("location") or []:
            location = entry.get("location") or {}
            if location.get("reference"):
                reference = location["reference"]
                display = location.get("display") or display
                break

    if not reference:
        return None, None
    return reference.split("/")[-1] or None, display


def write_care_site(conn: Any, resource: dict, storage_key: str) -> Optional[int]:
    """Upsert the care site an Encounter names, returning its care_site_id.

    Returns None when the Encounter names no facility, which is common and
    not an error - core/db/omop_schema.sql explains why visit_occurrence
    keeps a nullable care_site_id rather than inventing an "unknown" row.
    """
    source_id, display = _care_site_from_encounter(resource)
    if not source_id:
        return None

    care_site_id = deterministic_id("Organization", source_id)
    columns = [
        "care_site_id", "care_site_name", "care_site_source_value",
        "first_seen_storage_key",
    ]
    values = [care_site_id, display, source_id, storage_key]
    # Upserting refreshes the name, so the most recently seen label wins.
    # Deliberate: Epic display strings drift, and the newest is the best
    # guess at what the facility is currently called.
    _execute_upsert(conn, "cdm.care_site", columns, values, pk_column="care_site_id")
    return care_site_id


def _fhir_human_name(resource: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(family, given, full) from a FHIR Patient, preferring the official name.

    FHIR allows several names with a `use` - official, usual, nickname,
    maiden. A records search wants the official one, but a request may
    well arrive under a maiden name, so `full` is built from the chosen
    name while every OTHER name still reaches the searchable column via
    write_patient_identity below. Preferring `official` and silently
    dropping the rest would make exactly the search that matters most
    fail.
    """
    names = resource.get("name") or []
    if not names:
        return None, None, None

    def rank(entry: dict) -> int:
        return {"official": 0, "usual": 1, "maiden": 2}.get(entry.get("use") or "", 3)

    chosen = sorted(names, key=rank)[0]
    family = chosen.get("family")
    given = " ".join(chosen.get("given") or []) or None
    text = chosen.get("text")
    full = text or " ".join(p for p in [given, family] if p) or None
    return family, given, full


def write_patient_identity(conn: Any, resource: dict, storage_key: str) -> Optional[str]:
    """Write the searchable identity row for a FHIR Patient.

    Called only when the identity index is configured; a deployment that
    has not enabled it never reaches here and stores no names anywhere
    outside encrypted objects.

    NO IDENTIFIERS ARE COPIED. FHIR Patient.identifier routinely carries
    an MRN and can carry a social security number. None of it is written:
    a name search needs a name, and every identifier stored here would be
    one more directly-identifying field in the most sensitive table in the
    system for no gain in what the search can answer.
    """
    patient_id = resource["id"]
    family, given, full = _fhir_human_name(resource)
    if not full:
        # Nothing to search on. A Patient with no name at all is valid
        # FHIR; writing an empty row would only pad the table.
        return None

    # Every name the patient is recorded under, so a maiden-name request
    # resolves. Joined into the searchable text rather than stored as
    # separate rows: one row per patient keeps the primary key honest and
    # keeps a search from returning the same person several times.
    all_names = []
    for entry in resource.get("name") or []:
        parts = list(entry.get("given") or [])
        if entry.get("family"):
            parts.append(entry["family"])
        if entry.get("text"):
            parts.append(entry["text"])
        all_names.extend(parts)
    searchable = " ".join(dict.fromkeys(n for n in all_names if n)) or full

    person_id = deterministic_id("Patient", patient_id)
    birth_date = _parse_fhir_date(resource.get("birthDate"))

    columns = [
        "person_id", "patient_reference", "family_name", "given_names",
        "full_name", "birth_date", "gender", "source_storage_key",
    ]
    values = [
        person_id, f"Patient/{patient_id}", family, given,
        searchable, birth_date, resource.get("gender"), storage_key,
    ]
    return _execute_upsert(
        conn, "identity.patient_identity", columns, values, pk_column="person_id"
    )


# Remembered per connection object rather than looked up per Patient: the
# answer cannot change during a run, and a catalogue lookup on every row
# of a bulk ETL is a round trip nobody needs.
_IDENTITY_AVAILABLE: "dict[int, bool]" = {}


def _identity_index_available(conn: Any) -> bool:
    """Whether identity.patient_identity exists, asked once per connection.

    to_regclass() returns NULL for a missing relation instead of raising,
    which is the entire reason it is used here: asking the question must
    not itself put the transaction into a failed state.
    """
    cached = _IDENTITY_AVAILABLE.get(id(conn))
    if cached is not None:
        return cached

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT to_regclass('identity.patient_identity')")
        row = cursor.fetchone()
        available = bool(row and row[0] is not None)
    except Exception as exc:  # pragma: no cover - catalogue unreadable
        log.debug("could not check for the identity index: %s", exc)
        available = False
    finally:
        cursor.close()

    _IDENTITY_AVAILABLE[id(conn)] = available
    if not available:
        log.info(
            "identity.patient_identity is not installed, so name search is "
            "unavailable and no names will be indexed. This is the default; apply "
            "core/db/identity_schema.sql to enable it."
        )
    return available


def _write_identity_if_available(conn: Any, resource: dict, storage_key: str) -> None:
    """Write the identity row, if this deployment has the table for it.

    ASKED, NOT ATTEMPTED-AND-CAUGHT, and the difference is not stylistic.
    An earlier version wrapped the write in a SAVEPOINT so a missing table
    could not abort the batch. That was wrong twice over, and only a run
    against real PostgreSQL showed it:

      - It could not work. _execute_upsert() commits on success and rolls
        back on failure, and BOTH destroy every savepoint in the
        transaction - so the RELEASE that followed failed with "savepoint
        does not exist" and took down the very ETL it was meant to
        protect. Against a fake connection this looked fine.
      - It was not needed. Because _execute_upsert() already commits or
        rolls back per statement, a failed identity write never leaves the
        connection in an aborted state to begin with.

    Checking once for the table is simpler than either, and cannot fail
    the batch because nothing errors when the answer is no.
    """
    if not _identity_index_available(conn):
        return
    try:
        write_patient_identity(conn, resource, storage_key)
    except Exception as exc:
        # The table exists but this row would not write - a genuine fault
        # worth seeing, and survivable: _execute_upsert has already rolled
        # its own statement back, so the ETL continues. cdm.person is the
        # record that matters and it is already written.
        log.warning(
            "could not index the name for Patient %s: %s",
            resource.get("id"), exc,
        )
# Made by Ryan Gomez & Co. Inc.
