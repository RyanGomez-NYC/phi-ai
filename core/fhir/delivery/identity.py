# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Patient identity mapping across EMRs.

THE PROBLEM THIS EXISTS FOR. This platform holds records keyed by the
SOURCE EMR's patient id - `Patient/eAB12cd3` from an Epic instance.
Delivering those records into a different EMR requires that EMR's own id
for the same human being. The two ids have nothing to do with each other:
they are opaque, server-assigned, and unique to their issuing system.

WHY THIS IS NOT DONE BY MATCHING. Matching patients across systems on
name, date of birth and sex is an entire discipline (enterprise master
patient index) with a real, measured error rate. Two errors are possible
and they are not symmetric:

  - A false NEGATIVE creates a duplicate chart. Recoverable, annoying.
  - A false POSITIVE writes one person's medical history into another
    person's chart. That is a clinical safety incident and a HIPAA
    disclosure at once, and in a destination EMR it is very hard to fully
    unwind - the records are now in a live chart other clinicians read.

This module therefore does no matching at all. A mapping is SUPPLIED by
the operator and verified before any write. It is the same decision made
for OCR patient linkage in core/fhir/documents.py and for the same
reason: identity resolution belongs to a human or to a system built for
it, not to a side-effect of an export.

This platform additionally CANNOT match even if it wanted to: the
Postgres index holds no names, MRNs or dates of birth by design
(core/db/schema.sql). Demographics exist only inside encrypted Patient
resources. That constraint is load-bearing, not incidental.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("phi-ai.fhir.delivery.identity")

FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


class IdentityMappingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PatientMapping:
    """One human being, as identified by two systems."""

    source_patient_id: str      # as stored, e.g. "eAB12cd3"
    target_patient_id: str      # the destination EMR's own id
    verified_by: str            # who asserted these are the same person
    note: str = ""

    @property
    def source_reference(self) -> str:
        return f"Patient/{self.source_patient_id}"

    @property
    def target_reference(self) -> str:
        return f"Patient/{self.target_patient_id}"


class IdentityMap:
    """An explicit, operator-supplied set of patient mappings."""

    def __init__(self, mappings: list[PatientMapping]):
        self._by_source: dict[str, PatientMapping] = {}
        for mapping in mappings:
            if mapping.source_patient_id in self._by_source:
                raise IdentityMappingError(
                    f"source patient {mapping.source_patient_id} is mapped twice. One "
                    "source patient cannot be two people in the destination; resolve the "
                    "duplicate before delivering anything."
                )
            self._by_source[mapping.source_patient_id] = mapping

        # A target appearing twice means two source patients would be
        # merged into one destination chart. Occasionally legitimate
        # (a genuine duplicate in the source), but never something to do
        # silently.
        seen_targets: dict[str, str] = {}
        for mapping in mappings:
            previous = seen_targets.get(mapping.target_patient_id)
            if previous:
                log.warning(
                    "source patients %s and %s BOTH map to destination patient %s - their "
                    "records will be merged into one chart. Confirm this is intended.",
                    previous, mapping.source_patient_id, mapping.target_patient_id,
                )
            seen_targets[mapping.target_patient_id] = mapping.source_patient_id

    def __len__(self) -> int:
        return len(self._by_source)

    def resolve(self, source_reference: str) -> PatientMapping:
        """Map a source Patient reference, or refuse.

        Refusing is the whole point. A delivery that silently skipped
        unmapped patients would under-deliver without saying so; one that
        guessed would risk the false positive above.
        """
        source_id = (source_reference or "").split("/")[-1]
        mapping = self._by_source.get(source_id)
        if mapping is None:
            raise IdentityMappingError(
                f"no destination patient is mapped for {source_reference}. Delivery "
                "requires an explicit mapping per patient - this system does not match "
                "patients across EMRs, because a false match writes one person's history "
                "into another person's chart."
            )
        return mapping

    def has(self, source_reference: str) -> bool:
        return (source_reference or "").split("/")[-1] in self._by_source

    @property
    def source_ids(self) -> frozenset[str]:
        return frozenset(self._by_source)


def load_identity_map(path: str) -> IdentityMap:
    """Read a mapping CSV.

    CSV rather than YAML or JSON because this file is frequently produced
    by the destination EMR's own patient-matching or migration tooling,
    or by a records team working in a spreadsheet. Meeting them where
    they are matters more than format elegance.

    Required columns: source_patient_id, target_patient_id, verified_by.
    `verified_by` is required, not optional: a mapping with no named
    person behind it is an assertion nobody made.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise IdentityMappingError(f"no identity map at {path}")

    mappings: list[PatientMapping] = []
    with file_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = {"source_patient_id", "target_patient_id", "verified_by"} - set(
            reader.fieldnames or []
        )
        if missing:
            raise IdentityMappingError(
                f"{path} is missing required column(s): {', '.join(sorted(missing))}"
            )

        for line_number, row in enumerate(reader, start=2):
            source = (row.get("source_patient_id") or "").strip()
            target = (row.get("target_patient_id") or "").strip()
            verified_by = (row.get("verified_by") or "").strip()

            if not any((source, target, verified_by)):
                continue  # blank row

            for label, value in (("source_patient_id", source),
                                 ("target_patient_id", target)):
                if not value:
                    raise IdentityMappingError(f"{path} line {line_number}: {label} is empty")
                if not FHIR_ID.match(value):
                    raise IdentityMappingError(
                        f"{path} line {line_number}: {label} {value!r} is not a valid FHIR "
                        "id. Supply the EMR's own identifier, not an MRN or a name."
                    )
            if not verified_by:
                raise IdentityMappingError(
                    f"{path} line {line_number}: verified_by is required. A patient "
                    "mapping with nobody's name against it is an assertion nobody made."
                )

            mappings.append(
                PatientMapping(
                    source_patient_id=source,
                    target_patient_id=target,
                    verified_by=verified_by,
                    note=(row.get("note") or "").strip(),
                )
            )

    if not mappings:
        raise IdentityMappingError(f"{path} contains no mappings")

    log.info("loaded %d patient mapping(s) from %s", len(mappings), path)
    return IdentityMap(mappings)
# Made by Ryan Gomez & Co. Inc.
