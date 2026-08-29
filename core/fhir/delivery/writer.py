# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Deliver stored records into a destination EMR.

SOURCE AND TARGET ARE DIFFERENT SYSTEMS, AND THAT IS ENFORCED HERE.

  SOURCE system  the EMR where the data ORIGINATED. This project reads
                 from it and never writes to it. Not clinical data, not
                 corrections, not stored copies coming back.
  TARGET system  the EMR where the data ENDS ITS WORKFLOW. The only
                 system this module writes to.

A delivery pointed at a source EMR is refused outright - see
assert_not_source_system(). This platform exists because the source is
being retired; pushing stored records back into it would re-populate a
system someone is trying to switch off, and would do it with records that
have been through an export/import round trip. There is no override,
because there is no legitimate case for it in this design.

The ONLY requests this project makes to a source EMR are reads (paged
search, single resource read), an OAuth token request, and the documented
Bulk Data Delete that frees an export job WE asked it to create. That
last one mutates the source's job queue, never its clinical records.

READING FROM AN EMR AND WRITING INTO ONE ARE NOT SYMMETRIC OPERATIONS,
and most of this module is about that asymmetry.

Reading is safe: the worst outcome is an incomplete record set, discovered
and fixed by reading again. Writing puts historical records into a LIVE
CLINICAL CHART that other clinicians will read and act on. Three failure
modes matter, and none of them is theoretical:

  1. WRONG PATIENT. Handled by refusing to match patients at all - see
     core/fhir/delivery/identity.py. Every record needs an explicit,
     human-verified mapping or it is not sent.

  2. DUPLICATES. A delivery that runs twice writes everything twice
     unless the destination can express "create only if absent". Where
     conditional create is available it is used; where it is not, the
     delivery refuses to run unattended and requires an explicit
     acknowledgement, because a silent second copy of a patient's whole
     history is worse than a failed job.

  3. STALE DATA READ AS CURRENT. A 2019 observation appearing in a chart
     today, with no indication of where it came from, looks like it was
     recorded today. Every delivered resource is tagged with its origin
     and its retention provenance before it is sent. This is the change
     most likely to be considered optional and it is the one a clinician
     is most likely to be harmed by.

WHAT THIS WILL AND WILL NOT SEND. It writes only resource types the
destination's OWN CapabilityStatement advertises as creatable. The
per-vendor tables in core/fhir/emr_profiles.py are a planning aid, not
an authority - what a given health system's build accepts is theirs to
configure, and asking the server is the only honest way to know.

DRY RUN IS THE DEFAULT. Every delivery reports exactly what it would
send, per resource, before anything is written.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("phi-ai.fhir.delivery")


# Tag applied to every delivered resource. A destination clinician
# looking at an unfamiliar record should be able to see, from the record
# itself, that it came out of a prior-records system rather than being
# captured in their own.
def _prior_record_tag_system() -> str:
    """CodeSystem URL for the tag on every delivered record.

    Resolved at call time, not import time, so a deployment's configured
    namespace applies - see core/config/canonical.py. It is also used in
    the If-None-Exist header and by delivery verification, so all three
    must derive from one place or a re-run would duplicate records that
    verification then could not find.

    A deployment SHOULD set its own canonical namespace before it
    delivers anything, and must not change it afterwards: this value ends
    up inside another organisation's chart, and the conditional-create
    key below is matched against whatever was written the first time.
    """
    from core.config.canonical import code_system

    return code_system("record-origin")


# Kept as module attributes for callers that import them directly.
#
# Together they form the `If-None-Exist` conditional-create key that
# EMRWriter.deliver() sends to a LIVE DESTINATION EMR. That header is the
# entire mechanism that makes a repeated delivery safe - it tells the
# destination "create this only if you do not already have the record
# carrying this identifier". The destination matches it against the value
# stored when the record was FIRST delivered, so once a deployment has
# started delivering, changing either value would make that match fail
# and a re-run would re-create a patient's entire delivered history as
# duplicates.
PRIOR_RECORD_TAG_SYSTEM = _prior_record_tag_system()
PRIOR_RECORD_TAG_CODE = "prior-record"


class DeliveryError(RuntimeError):
    pass


class SourceSystemWriteRefused(DeliveryError):
    """A delivery was pointed at an EMR this platform reads from.

    Its own class so this can never be caught by a generic handler that
    treats delivery failures as retryable. This one is not retryable; it
    is a configuration error with clinical consequences.
    """


def _normalise_emr_url(url: str) -> str:
    """Scheme+host+port+path, lowercased, no trailing slash.

    Compared at this granularity rather than by host alone because a
    multi-tenant vendor puts the tenant in the path - two Cerner tenants
    share a host and are entirely different systems. Comparing hosts
    would refuse legitimate deliveries between tenants; comparing full
    URLs would let a trailing slash defeat the check.
    """
    from urllib.parse import urlparse

    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    return f"{(parsed.scheme or '').lower()}://{host}{path}"


def assert_not_source_system(destination_url: str, source_urls) -> None:
    """Refuse a delivery aimed at a system this platform reads from.

    Checked before authentication, before any capability lookup, and
    before a single resource is prepared - the earliest point at which the
    mistake is visible, so nothing is half-done when it is caught.
    """
    destination = _normalise_emr_url(destination_url)
    for source in source_urls or ():
        if not source:
            continue
        if destination == _normalise_emr_url(source):
            raise SourceSystemWriteRefused(
                f"{destination_url} is a SOURCE system for this platform - the EMR the "
                "data came from. This project reads from source systems and never writes "
                "to them.\n\n"
                "This platform exists because that system is being retired. Pushing "
                "stored records back into it would re-populate a system someone is "
                "switching off, using records that have been through an export/import "
                "round trip.\n\n"
                "Deliver to the TARGET system - the EMR where this data ends its "
                "workflow - instead. There is no override for this."
            )


@dataclass
class DeliveryItem:
    resource_type: str
    source_id: str
    storage_key: str
    source_patient: str
    target_patient: str
    sent: bool = False
    skipped_reason: Optional[str] = None
    destination_id: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DeliveryResult:
    destination: str
    dry_run: bool
    items: list[DeliveryItem] = field(default_factory=list)

    @property
    def sent_count(self) -> int:
        return sum(1 for i in self.items if i.sent)

    @property
    def skipped_count(self) -> int:
        return sum(1 for i in self.items if i.skipped_reason)

    @property
    def failed(self) -> list[DeliveryItem]:
        return [i for i in self.items if i.error]


def tag_as_prior_record(
    resource: dict,
    source_system: str,
    stored_storage_key: str,
    source_patient_reference: str,
) -> dict:
    """Return a copy marked as a prior record from another system.

    Uses meta.source and meta.tag, both standard FHIR R4. A destination
    that ignores meta still stores it, so the provenance survives even
    where the receiving UI does not surface it.

    The original source patient reference is preserved in an identifier-
    style extension rather than discarded: when someone in the
    destination later asks "where did this come from", the answer needs
    to include which patient it was in the originating system, or the
    record cannot be traced back to the object it was served from.

    The `display` text is prose a clinician reads in their own chart, so
    it names the platform the record came from rather than a storage
    tier.
    """
    delivered = json.loads(json.dumps(resource))  # deep copy; never mutate the stored copy
    meta = delivered.setdefault("meta", {})

    meta["source"] = f"{source_system}#{stored_storage_key}"

    tags = meta.setdefault("tag", [])
    if not any(t.get("system") == PRIOR_RECORD_TAG_SYSTEM for t in tags):
        tags.append({
            "system": PRIOR_RECORD_TAG_SYSTEM,
            "code": PRIOR_RECORD_TAG_CODE,
            "display": "Historical record delivered from the PHI AI Platform, not captured in this system",
        })

    extensions = delivered.setdefault("extension", [])
    extensions.append({
        "url": f"{PRIOR_RECORD_TAG_SYSTEM}/source-patient",
        "valueString": source_patient_reference,
    })
    return delivered


def repoint_to_target_patient(resource: dict, target_reference: str) -> dict:
    """Rewrite the patient link to the destination's own id.

    Only `subject` and `patient` are rewritten - the two fields
    core/db/index.py already treats as the patient link, so the same
    definition of "which patient" applies on the way out as on the way
    in.

    OTHER references are deliberately NOT rewritten. An Observation
    referencing `Encounter/enc1` still points at the SOURCE system's
    encounter, which does not exist in the destination. Silently
    stripping those would quietly discard clinical context; silently
    keeping them leaves a dangling reference. This function keeps them,
    and the delivery report names them, so a human decides. Rewriting a
    whole reference graph across systems is a migration project, not a
    side effect of an export.
    """
    delivered = dict(resource)
    for field_name in ("subject", "patient"):
        value = delivered.get(field_name)
        if isinstance(value, dict) and str(value.get("reference", "")).startswith("Patient/"):
            delivered[field_name] = {**value, "reference": target_reference}
    return delivered


def dangling_references(resource: dict) -> list[str]:
    """Non-patient references that will not resolve in the destination."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            reference = node.get("reference")
            if isinstance(reference, str) and "/" in reference:
                if not reference.startswith("Patient/"):
                    found.append(reference)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(resource)
    return sorted(set(found))


class EMRWriter:
    """
    Writes resources into a destination EMR over FHIR REST.

    `http_post`/`http_get` are injected so delivery is testable without a
    live EMR - and, more importantly, so the tests cannot accidentally
    write to one.
    """

    def __init__(
        self,
        base_url: str,
        access_token: str,
        profile,
        audit,
        actor: str = "phi-ai-delivery",
        http_post=None,
        http_get=None,
        source_system_urls=None,
    ):
        # Refused at CONSTRUCTION, not at first write: an EMRWriter aimed
        # at a source system should not exist at all, and failing here
        # means no token is obtained and no capability request is made
        # against a system that should only ever be read.
        assert_not_source_system(base_url, source_system_urls or ())
        self.source_system_urls = tuple(source_system_urls or ())
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.profile = profile
        self.audit = audit
        self.actor = actor
        self._http_post = http_post or _default_post
        self._http_get = http_get or _default_get
        self._capabilities: Optional[frozenset[str]] = None

    # -- capability discovery ---------------------------------------

    def creatable_resource_types(self) -> frozenset[str]:
        """What the destination itself says it will accept a create for.

        Asked once and cached. The vendor table in emr_profiles.py is a
        planning aid; this is the authority, because what a health system's
        build has enabled is theirs to configure and only their server
        knows it.
        """
        if self._capabilities is not None:
            return self._capabilities

        try:
            statement = self._http_get(f"{self.base_url}/metadata")
        except Exception as exc:
            raise DeliveryError(
                f"could not read the destination's CapabilityStatement: {exc}. Refusing to "
                "write - guessing what a live clinical system accepts is not acceptable."
            ) from exc

        creatable: set[str] = set()
        for rest in statement.get("rest", []):
            for resource in rest.get("resource", []):
                rtype = resource.get("type")
                interactions = {i.get("code") for i in resource.get("interaction", [])}
                if rtype and "create" in interactions:
                    creatable.add(rtype)

        self._capabilities = frozenset(creatable)
        log.info("destination advertises create for %d resource type(s)", len(creatable))
        return self._capabilities

    # -- delivery ----------------------------------------------------

    def deliver(
        self,
        resources: list[tuple[dict, dict]],
        identity_map,
        source_system: str,
        purpose_of_use: str,
        dry_run: bool = True,
        allow_duplicates: bool = False,
    ) -> DeliveryResult:
        """
        `resources` is a list of (index_row, resource) pairs.

        Nothing is written when dry_run is True, which is the default.
        """
        creatable = self.creatable_resource_types()
        result = DeliveryResult(destination=self.base_url, dry_run=dry_run)

        if not self.profile.supports_conditional_create and not dry_run and not allow_duplicates:
            raise DeliveryError(
                f"{self.profile.name} does not support conditional create, so this delivery "
                "cannot tell an already-sent record from a new one. Running it twice would "
                "duplicate every record in a live chart. Re-run with allow_duplicates=True "
                "only if you have confirmed externally that these records are not already "
                "there."
            )

        for row, resource in resources:
            rtype = resource.get("resourceType", "?")
            item = DeliveryItem(
                resource_type=rtype,
                source_id=str(resource.get("id", "?")),
                storage_key=row.get("storage_key", ""),
                source_patient=row.get("patient_reference") or "",
                target_patient="",
            )

            if rtype not in creatable:
                item.skipped_reason = (
                    f"the destination does not advertise create for {rtype}"
                )
                result.items.append(item)
                continue

            try:
                mapping = identity_map.resolve(item.source_patient)
            except Exception as exc:
                item.skipped_reason = str(exc)
                result.items.append(item)
                continue

            item.target_patient = mapping.target_reference

            prepared = tag_as_prior_record(
                resource,
                source_system=source_system,
                stored_storage_key=item.storage_key,
                source_patient_reference=item.source_patient,
            )
            prepared = repoint_to_target_patient(prepared, mapping.target_reference)

            if dry_run:
                item.status = "would send"
                result.items.append(item)
                continue

            # Audited BEFORE the write, matching every other outbound
            # path in this codebase: a failure leaves evidence of an
            # attempted disclosure rather than none.
            self.audit.record(
                actor=self.actor,
                action="record.deliver",
                resource_key=f"{item.storage_key} -> {self.base_url} {mapping.target_reference}",
                purpose_of_use=purpose_of_use,
            )

            try:
                headers = {
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/fhir+json",
                    "Accept": "application/fhir+json",
                }
                if self.profile.supports_conditional_create:
                    # The mechanism that makes a re-run safe: the server
                    # creates only if nothing matches this platform's
                    # record for this patient. PRIOR_RECORD_TAG_SYSTEM's
                    # VALUE is what the destination matched on the first
                    # time, which is why a deployment must settle its
                    # canonical namespace before it delivers anything.
                    headers["If-None-Exist"] = (
                        f"identifier={PRIOR_RECORD_TAG_SYSTEM}|{item.storage_key}"
                        f"&patient={mapping.target_reference}"
                    )

                response = self._http_post(
                    f"{self.base_url}/{rtype}", prepared, headers
                )
                item.sent = True
                item.destination_id = response.get("id")
                item.status = response.get("_status", "created")
            except Exception as exc:
                item.error = str(exc)
                log.error("delivery failed for %s: %s", item.storage_key, exc)

            result.items.append(item)

        return result


def _default_post(url: str, resource: dict, headers: dict) -> dict:
    import requests

    response = requests.post(url, json=resource, headers=headers, timeout=60)
    response.raise_for_status()
    body = response.json() if response.content else {}
    # 200 with no create means the conditional create matched an existing
    # record - a successful no-op, not a new write, and the report should
    # say which happened.
    body["_status"] = "already present" if response.status_code == 200 else "created"
    return body


def _default_get(url: str) -> dict:
    import requests

    response = requests.get(url, headers={"Accept": "application/fhir+json"}, timeout=30)
    response.raise_for_status()
    return response.json()
# Made by Ryan Gomez & Co. Inc.
