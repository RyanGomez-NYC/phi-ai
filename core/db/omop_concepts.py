# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Concept mapping for the OMOP CDM analytics layer.

OMOP represents every clinical fact as a Standard Concept, not a raw
source code - see core/db/omop_schema.sql's own header for why this
project cannot bundle the vocabulary data that makes that possible
(OHDSI's Athena repository, per-vocabulary licensed) and
core/db/omop_vocab_schema.sql for the (currently empty, structure-only)
table this module queries once that data is loaded.

TWO KINDS OF MAPPING IN THIS MODULE, kept deliberately separate:

  1. A small, HARDCODED table for the handful of concepts that are
     fixed, universal, and independently verified against multiple
     OHDSI sources - not the kind of thing that varies by vocabulary
     version or needs Athena at all. Currently just gender: Male
     (8507), Female (8532), Unknown (8551) - confirmed consistently
     across OHDSI's own "Book of OHDSI" documentation, the HL7
     FHIR-to-OMOP Implementation Guide, the OHDSI/OMOP-Queries
     reference query library, and OHDSI's own FhirToCdm reference ETL
     tool. "Other" is deliberately NOT hardcoded here - sources
     disagree on which concept_id to use (44814653 per the FHIR-to-OMOP
     IG; 8521 is listed elsewhere as a non-standard source concept with
     no mapping) - falls through to lookup_concept()'s ordinary
     unmapped (0) behavior rather than asserting a value this module
     isn't actually confident in.

  2. Everything else (diagnosis codes, drug codes, lab codes, and race/
     ethnicity - the CDC Race and Ethnicity Code System is itself
     distributed as part of the OMOP standard vocabularies, not
     something to hand-hardcode) goes through lookup_concept() against
     vocab.concept. Confirmed against OHDSI's own reference FhirToCdm
     tool as the right general shape: that implementation hardcodes
     gender identically to this module, and extracts race as a source
     value for mapping elsewhere - not a value it invents locally
     either.

concept_id = 0 is OMOP's own convention for "source code has no
Standard Concept mapping" - expected, routine output while the
vocabulary is empty or incomplete, not an error this module raises or
logs specially.
"""

from __future__ import annotations

from typing import Any, Optional

# Verified against multiple independent OHDSI sources - see this
# module's own docstring. Keyed on FHIR's own fixed gender codes
# (http://hl7.org/fhir/ValueSet/administrative-gender): male | female |
# other | unknown.
_GENDER_CONCEPT_IDS: dict[str, int] = {
    "male": 8507,
    "female": 8532,
    "unknown": 8551,
    # "other" deliberately absent - see module docstring. Falls through
    # to the unmapped default (0) in gender_concept_id() below.
}

# US Core race/ethnicity extension URLs - a stable, well-established
# FHIR US Core convention (these are fixed StructureDefinition
# canonical URLs, not something that varies by EMR or by deployment).
_US_CORE_RACE_EXTENSION_URL = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race"
_US_CORE_ETHNICITY_EXTENSION_URL = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity"
# Within each of the extensions above, the specific sub-extension URL
# carrying the actual OMB/CDC category coding (as opposed to
# "detailed" or "text", the extension's other two permitted
# sub-elements).
_OMB_CATEGORY_URL = "ombCategory"


def gender_concept_id(fhir_gender: Optional[str]) -> int:
    """
    Maps a FHIR Patient.gender code to OMOP's gender_concept_id.
    gender_concept_id is NOT NULL in cdm.person
    (core/db/omop_schema.sql) - OMOP requires a value even where FHIR
    permits the field to be entirely absent, so a missing or
    unrecognized source value maps to Unknown (8551) rather than
    raising or leaving the field genuinely empty.
    """
    if not fhir_gender:
        return _GENDER_CONCEPT_IDS["unknown"]
    return _GENDER_CONCEPT_IDS.get(fhir_gender.lower(), _GENDER_CONCEPT_IDS["unknown"])


def extract_us_core_coding(resource: dict, extension_url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Pulls the (system, code) pair out of a US Core race/ethnicity
    ombCategory sub-extension, if present. Returns (None, None) if the
    resource carries no such extension - common and expected (race/
    ethnicity are optional, sometimes-declined-to-answer fields in a
    real EMR), not an error.

    US Core structures race/ethnicity as a top-level extension
    containing one or more ombCategory sub-extensions (a patient can
    have more than one race recorded) plus optional detailed/text
    sub-extensions. This function returns only the FIRST ombCategory
    coding - a real simplification for a patient with multiple
    recorded races, flagged here rather than silently handled: fuller
    multi-race support would return every ombCategory entry, not just
    one, and is a reasonable follow-on refinement rather than done in
    this pass.
    """
    for ext in resource.get("extension", []):
        if ext.get("url") != extension_url:
            continue
        for sub_ext in ext.get("extension", []):
            if sub_ext.get("url") != _OMB_CATEGORY_URL:
                continue
            coding = sub_ext.get("valueCoding", {})
            return coding.get("system"), coding.get("code")
    return None, None


def lookup_concept(
    conn: Any,
    vocabulary_id: str,
    concept_code: str,
    prefer_standard: bool = True,
) -> int:
    """
    Looks up a source (vocabulary_id, concept_code) pair against
    vocab.concept (core/db/omop_vocab_schema.sql), returning its
    concept_id - or 0 (OMOP's "unmapped" convention) if no row is
    found, which includes the routine case of the vocabulary table
    being empty because Athena data hasn't been loaded yet.

    A (vocabulary_id, concept_code) pair can genuinely match more than
    one row (see omop_vocab_schema.sql's own note on why this isn't
    declared UNIQUE) - when that happens and prefer_standard is true
    (the default), a row with standard_concept = 'S' is preferred over
    a non-standard match, since OMOP's own convention is that
    domain-table concept_id fields should hold a Standard Concept where
    one exists. Multiple remaining matches after that preference is
    applied resolve to whichever the query returns first - a genuine
    simplification worth revisiting once real vocabulary data and real
    ETL volume surface whether that ambiguity matters in practice,
    rather than solved speculatively now.

    Requires the caller's role to hold SELECT on vocab.concept - both
    omop_etl and omop_analyst do (core/db/omop_bootstrap_aws.sql) -
    this is reference terminology data, not PHI, so unlike the narrow
    column-scoped grants on cdm.person/visit_occurrence, full-table
    SELECT here carries none of the same minimum-necessary concern.
    """
    if not vocabulary_id or not concept_code:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT concept_id, standard_concept
            FROM vocab.concept
            WHERE vocabulary_id = %s AND concept_code = %s
            """,
            (vocabulary_id, concept_code),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    if prefer_standard:
        for concept_id, standard_concept in rows:
            if standard_concept == "S":
                return concept_id

    return rows[0][0]


def race_concept_id(conn: Any, resource: dict) -> tuple[int, Optional[str]]:
    """
    Returns (race_concept_id, race_source_value) for a FHIR Patient
    resource, via the US Core race extension. The CDC Race and
    Ethnicity Code System (the system US Core's ombCategory codings
    use) is itself distributed as part of the OMOP standard
    vocabularies - looked up through lookup_concept() like any other
    source code, not hardcoded here. Returns (0, None) if the resource
    carries no race extension at all - a real, common, non-error case.
    """
    system, code = extract_us_core_coding(resource, _US_CORE_RACE_EXTENSION_URL)
    if not code:
        return 0, None
    return lookup_concept(conn, vocabulary_id="Race", concept_code=code), code


def ethnicity_concept_id(conn: Any, resource: dict) -> tuple[int, Optional[str]]:
    """Same as race_concept_id() above, for the US Core ethnicity extension."""
    system, code = extract_us_core_coding(resource, _US_CORE_ETHNICITY_EXTENSION_URL)
    if not code:
        return 0, None
    return lookup_concept(conn, vocabulary_id="Ethnicity", concept_code=code), code
# Made by Ryan Gomez & Co. Inc.
