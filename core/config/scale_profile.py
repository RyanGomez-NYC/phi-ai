# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Scale profile: one deployment shape or the other, chosen deliberately.

WHY THIS EXISTS. A 500 GB deployment and a 100 TB deployment are not the
same system with different numbers. Below a few hundred million
resources, one object per resource is the right design: per-resource
integrity digests, per-resource disposal, a simple index. Above roughly a
billion, that same design produces tens of billions of objects and a
multi-terabyte index, and no amount of instance sizing fixes it - the
storage MODEL is what is wrong.

Rather than pick one and make the other deployment suffer, the shape is
selected at deployment time and the rest of the system reads it from here.

    PHI_AI_PROFILE=small        (default)
    PHI_AI_PROFILE=large

Read through core/config/settings.py's env_var(), which is the single
place this project resolves a deployment variable - see that module's
docstring for why nothing here reads os.environ directly.

THE PROFILE IS NOT A TUNING KNOB. It determines the storage layout, which
determines the object keys, which determines what is written to storage.
Changing it on a populated deployment does not migrate anything - it
changes where NEW resources go while leaving existing ones where they
are. Choose before ingesting, and see docs/SCALING.md before choosing.

THE DECIDING NUMBER IS RESOURCE COUNT, NOT BYTES. A 100 TB imaging
deployment can be ~1 million objects and belongs on `small`. A 20 TB
deployment of discrete FHIR resources can be 8 billion objects and
belongs on `large`. Estimate the count, not the terabytes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.config.settings import env_var

log = logging.getLogger("phi-ai.config.profile")


class StorageLayout(str, Enum):
    """How stored resources map onto storage objects."""

    PER_RESOURCE = "per_resource"
    # One object per resource: fhir/{Type}/{id}.json
    #
    # Per-resource integrity digests, per-resource disposal, and an index
    # row per resource. Everything is at its finest granularity, which is
    # exactly right until the object count makes it impossible.

    BUNDLED = "bundled"
    # One NDJSON object per (patient, resource type):
    # fhir/{Type}/{patientId}.ndjson
    #
    # Roughly 500x fewer objects at a large IDN's resource counts, and the
    # index drops from a row per resource to a row per bundle.
    #
    # Bundling on (patient, TYPE) rather than patient alone is deliberate:
    # retention is configurable per resource type
    # (retention_years_overrides), so a patient-only bundle would mix
    # retention periods inside one object and make disposal express
    # something the configuration cannot.


class IndexPartitioning(str, Enum):
    NONE = "none"
    BY_RESOURCE_TYPE = "by_resource_type"
    # LIST partitioning on resource_type. Chosen over hashing the patient
    # reference because the type set is small, known and stable, so the
    # partition list is bounded and readable - and because retention and
    # disposal are already expressed per resource type, so partitions line
    # up with the operations that scan them.
    #
    # The cost, stated plainly: a restore-by-patient query touches every
    # partition rather than one. Each is a fraction of the size and
    # carries its own patient index, so this is a smaller penalty than
    # the alternative's - hash-by-patient would make find_by_type scan
    # everything, and that is the query disposal runs.


@dataclass(frozen=True)
class ScaleProfile:
    name: str
    storage_layout: StorageLayout
    index_partitioning: IndexPartitioning

    # Resources per bundle before a new one is started. Only meaningful
    # under BUNDLED. Sized so a bundle comfortably exceeds the 128 KB
    # minimum that cold storage tiers bill per object - below that,
    # lifecycle transitions cost more than they save.
    max_bundle_resources: int = 5_000

    guidance: str = ""

    @property
    def bundles(self) -> bool:
        return self.storage_layout is StorageLayout.BUNDLED

    @property
    def partitions_index(self) -> bool:
        return self.index_partitioning is not IndexPartitioning.NONE

    @property
    def schema_file(self) -> str:
        return ("core/db/schema_partitioned.sql" if self.partitions_index
                else "core/db/schema.sql")


SMALL = ScaleProfile(
    name="small",
    storage_layout=StorageLayout.PER_RESOURCE,
    index_partitioning=IndexPartitioning.NONE,
    guidance=(
        "One object per resource. Per-resource integrity digests and "
        "per-resource disposal. Right up to roughly 200-500 million resources; "
        "past that the object count and index size start to dominate."
    ),
)

LARGE = ScaleProfile(
    name="large",
    storage_layout=StorageLayout.BUNDLED,
    index_partitioning=IndexPartitioning.BY_RESOURCE_TYPE,
    guidance=(
        "NDJSON bundles per (patient, resource type), and a partitioned index. "
        "Roughly 500x fewer objects at a large IDN's counts. Integrity and "
        "disposal move from per-resource to per-bundle - see docs/SCALING.md "
        "for what that costs."
    ),
)

PROFILES: dict[str, ScaleProfile] = {"small": SMALL, "large": LARGE}

# A count past which `small` stops being a reasonable choice. Not enforced
# - an operator may have reasons - but warned about, because the failure
# is gradual and easy to miss until reconciliation stops completing.
LARGE_PROFILE_THRESHOLD = 500_000_000


def profile_from_env() -> ScaleProfile:
    raw = (env_var("PROFILE") or "small").strip().lower()
    if raw not in PROFILES:
        raise ValueError(
            f"PHI_AI_PROFILE={raw!r} is not valid. Choose 'small' or 'large' - "
            "see docs/SCALING.md. This is a deployment shape, not a tuning knob: it "
            "determines the storage layout and cannot be changed on a populated "
            "deployment without migrating it."
        )
    return PROFILES[raw]


def warn_if_undersized(profile: ScaleProfile, resource_count: Optional[int]) -> Optional[str]:
    """Flag a `small` profile carrying more than it should.

    Returned rather than raised: an operator may be mid-migration, or may
    have a good reason. What is not acceptable is finding out only when
    reconciliation stops completing.
    """
    if profile.bundles or resource_count is None:
        return None
    if resource_count < LARGE_PROFILE_THRESHOLD:
        return None
    message = (
        f"This deployment holds {resource_count:,} resources on the 'small' profile, past "
        f"the ~{LARGE_PROFILE_THRESHOLD:,} point where one-object-per-resource starts to "
        "dominate cost and verification time. Switching profiles does not migrate "
        "existing objects - see docs/SCALING.md."
    )
    log.warning(message)
    return message
# Made by Ryan Gomez & Co. Inc.
