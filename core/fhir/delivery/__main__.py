# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Deliver stored records into a destination EMR.

    # Always dry-run first. This is the default; --confirm is required to write.
    python -m core.fhir.delivery \\
        --destination https://fhir.cerner.example/r4 \\
        --vendor cerner \\
        --identity-map ./patient-mapping.csv \\
        --patient eAB12cd3 \\
        --purpose-of-use "Continuity of care, patient transferred to Example Health"

    # Every mapped patient, rather than one.
    python -m core.fhir.delivery --destination ... --vendor cerner \\
        --identity-map ./patient-mapping.csv --all-mapped --purpose-of-use "..." --confirm

BULK HERE MEANS "EVERY MAPPED PATIENT", NOT "EVERYTHING STORED", and the
difference is deliberate. Delivery is bounded by the identity map, so the
upper bound on a bulk run is exactly the set of patients a human verified.
There is no way to accidentally push an entire record set into a live
clinical system, because there is no code path that resolves a patient
without a mapping.

For moving everything stored to a system that is NOT a live EMR - a
successor platform, a data warehouse, a vendor's own migration tooling -
use core/fhir/bulk_export.py instead. It produces NDJSON in the standard
FHIR Bulk Data shape, needs no destination credentials, and no
identity mapping, because it is not writing into anyone's chart.

The destination token request follows the DESTINATION's profile:
its auth_flow (assertion or secret), its assertion_algorithm (the key in
PHI_AI_DELIVERY_PRIVATE_KEY_PATH must be of that family), its kid
(PHI_AI_DELIVERY_JWT_KID, when the registration pinned one) and, where
the profile records explicit scopes as mandatory, one
system/{Type}.write scope per writable resource type - see
delivery_scope(). A secret set for a vendor whose profile takes none is
refused, never silently used.

EVERY VARIABLE HERE IS READ THROUGH env_var(). PHI_AI_SOURCE_EMR_URLS is
the one that matters most: it extends the list EMRWriter refuses to write
into (assert_not_source_system()). A read that misses it does not fail -
it silently narrows a safety check on the only code path in this project
that writes into a live clinical system.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from core.config.settings import env_var

log = logging.getLogger("phi-ai.fhir.delivery.cli")


def _build(args):
    from core.audit.log import AuditLog
    from core.config.settings import Settings
    from core.crypto.envelope import EnvelopeEncryptor
    from core.db.connection import connect
    from core.fhir.emr_profiles import profile_for
    from core.storage.factory import build_audit_sink, build_kms, build_storage
    from core.web.data import LiveRecordReader

    settings = Settings.from_env()
    storage = build_storage(settings)
    encryptor = EnvelopeEncryptor(kms=build_kms(settings))
    sink = build_audit_sink(settings)
    audit = AuditLog(sink=sink, last_known_hash=sink.last_hash())

    reader = LiveRecordReader(
        # Reader: delivery reads stored resources out and writes them to
        # a destination EMR over FHIR. It touches no table but
        # stored_resources, and only to SELECT.
        connection_factory=lambda: connect(settings, settings.db_reader_username),
        storage=storage,
        encryptor=encryptor,
        audit_sink=sink,
    )
    return settings, reader, audit, profile_for(args.vendor)


def delivery_scope(profile) -> Optional[str]:
    """The scope string the destination token request carries, derived
    from the DESTINATION profile the way authenticate_from_settings()
    derives the ingestion one: where the profile records explicit scopes
    as mandatory, one system/{Type}.write per writable resource type
    (SMART v1 grammar, the same family as the .read scopes the ingestion
    side sends); otherwise no scope parameter at all. A profile that
    requires scopes but is writable for nothing gets no scope and the
    writer refuses every type on the CapabilityStatement anyway.
    """
    if profile.requires_token_scopes and profile.writable_resources:
        return " ".join(f"system/{rtype}.write" for rtype in profile.writable_resources)
    return None


def _access_token(args, profile) -> str:
    """Obtain a token for the DESTINATION, authenticating the way the
    destination's own profile documents: the client is built on that
    profile (so the assertion is signed with its assertion_algorithm and
    the key is checked against that family first), the grant is the
    profile's auth_flow, and a client secret is refused loudly for a
    vendor whose profile does not take one - mirroring
    authenticate_from_settings() and Settings.from_env() on the ingestion
    side, so a stale PHI_AI_DELIVERY_CLIENT_SECRET in the environment
    can never silently swap a JWT vendor onto the secret path.

    Reads the secret from the environment, never an argument: a client
    secret on a command line lands in shell history and in every process
    listing on the host.
    """
    from core.fhir.client import (
        ClientAssertionKeyError,
        FHIRIngestionClient,
        check_private_key_signs,
    )

    token = env_var("DELIVERY_ACCESS_TOKEN")
    if token:
        return token

    client_id = env_var("DELIVERY_CLIENT_ID")
    token_url = env_var("DELIVERY_TOKEN_URL")
    if not client_id or not token_url:
        raise SystemExit(
            "No destination credentials. Set PHI_AI_DELIVERY_ACCESS_TOKEN, or "
            "PHI_AI_DELIVERY_CLIENT_ID + PHI_AI_DELIVERY_TOKEN_URL with either "
            "PHI_AI_DELIVERY_CLIENT_SECRET (a vendor whose profile records "
            "auth_flow=oauth2_client_credentials) or "
            "PHI_AI_DELIVERY_PRIVATE_KEY_PATH (SMART backend services)."
        )

    client = FHIRIngestionClient(
        base_url=args.destination, profile=profile, storage=None, encryptor=None,
        audit=None, retention_years=0,
    )
    scope = delivery_scope(profile)
    secret = env_var("DELIVERY_CLIENT_SECRET")
    if profile.auth_flow == "oauth2_client_credentials":
        if not secret:
            raise SystemExit(
                f"{profile.name} authenticates with a client secret; set "
                "PHI_AI_DELIVERY_CLIENT_SECRET"
            )
        client.authenticate_client_secret(client_id, secret, token_url, scope=scope)
        return client.access_token

    if secret:
        raise SystemExit(
            f"{profile.name} does not accept a client secret (its profile records "
            f"auth_flow={profile.auth_flow!r}); unset PHI_AI_DELIVERY_CLIENT_SECRET rather "
            "than let it override the signed-assertion grant this vendor documents"
        )
    key_path = env_var("DELIVERY_PRIVATE_KEY_PATH")
    if not key_path:
        raise SystemExit("PHI_AI_DELIVERY_PRIVATE_KEY_PATH is required for this vendor")
    with open(key_path, "rb") as handle:
        private_key_pem = handle.read()
    try:
        check_private_key_signs(profile.assertion_algorithm, private_key_pem)
    except ClientAssertionKeyError as exc:
        raise SystemExit(
            f"{profile.name} signs its client assertion with {profile.assertion_algorithm}, "
            f"but PHI_AI_DELIVERY_PRIVATE_KEY_PATH ({key_path!r}) cannot: {exc}"
        ) from exc
    client.authenticate(client_id, private_key_pem, token_url,
                        jwt_kid=env_var("DELIVERY_JWT_KID") or None, scope=scope)
    return client.access_token


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        prog="python -m core.fhir.delivery",
        description="Deliver stored records into a destination EMR.",
    )
    parser.add_argument("--destination", required=True, help="destination FHIR base URL")
    from core.fhir.emr_profiles import PROFILES

    parser.add_argument("--vendor", required=True, choices=sorted(PROFILES),
                        metavar="VENDOR",
                        help="the destination's profile key in core/fhir/emr_profiles.py "
                             "PROFILES: " + " | ".join(sorted(PROFILES)))
    parser.add_argument("--identity-map", required=True,
                        help="CSV mapping source patient ids to destination patient ids")
    parser.add_argument("--purpose-of-use", required=True,
                        help="recorded on every audit entry for this delivery")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--patient", help="one source patient id")
    scope.add_argument("--all-mapped", action="store_true",
                       help="every patient in the identity map")
    parser.add_argument("--confirm", action="store_true",
                        help="actually write. Without this nothing is sent.")
    parser.add_argument("--allow-duplicates", action="store_true",
                        help="proceed on a destination with no conditional create. Only "
                             "when you have confirmed externally that these records are "
                             "not already present.")
    args = parser.parse_args(argv)

    from core.fhir.delivery.identity import IdentityMappingError, load_identity_map
    from core.fhir.delivery.writer import (
        DeliveryError,
        EMRWriter,
        SourceSystemWriteRefused,
    )

    try:
        identity_map = load_identity_map(args.identity_map)
    except IdentityMappingError as exc:
        print(f"identity map: {exc}", file=sys.stderr)
        return 2

    settings, reader, audit, profile = _build(args)

    if args.patient:
        if not identity_map.has(args.patient):
            print(f"{args.patient} is not in the identity map.", file=sys.stderr)
            return 2
        source_ids = [args.patient.split("/")[-1]]
    else:
        source_ids = sorted(identity_map.source_ids)

    resources: list[tuple[dict, dict]] = []
    for source_id in source_ids:
        for row in reader.resources_for_patient(f"Patient/{source_id}"):
            # One key may hold a bundle - see LiveRecordReader.read_resources.
            for resource in reader.read_resources(row["storage_key"]):
                resources.append((row, resource))

    if not resources:
        print("Nothing stored for the requested patient(s).", file=sys.stderr)
        return 0

    # Every EMR this platform reads from. Delivery to any of them is
    # refused - see assert_not_source_system(). env_var(), not
    # os.environ.get(): this list is a safety boundary, and a read that
    # misses the operator's variable narrows it without saying so.
    source_urls = [settings.fhir_base_url]
    extra = env_var("SOURCE_EMR_URLS", "") or ""
    source_urls += [u.strip() for u in extra.split(",") if u.strip()]

    try:
        writer = EMRWriter(
            base_url=args.destination,
            access_token=_access_token(args, profile) if args.confirm else "dry-run",
            profile=profile,
            audit=audit,
            source_system_urls=source_urls,
        )
    except SourceSystemWriteRefused as exc:
        print(f"\nREFUSED\n\n{exc}\n", file=sys.stderr)
        return 4

    try:
        result = writer.deliver(
            resources=resources,
            identity_map=identity_map,
            source_system=settings.fhir_base_url or "phi-ai-platform",
            purpose_of_use=args.purpose_of_use,
            dry_run=not args.confirm,
            allow_duplicates=args.allow_duplicates,
        )
    except DeliveryError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    header = "DRY RUN - nothing was written." if result.dry_run else "Delivered."
    print(f"\n{header}")
    print(f"  destination   {result.destination} ({profile.name})")
    print(f"  patients      {len(source_ids)}")
    print(f"  considered    {len(result.items)}")
    print(f"  {'would send' if result.dry_run else 'sent':<13} {result.sent_count if not result.dry_run else sum(1 for i in result.items if i.status == 'would send')}")
    print(f"  skipped       {result.skipped_count}")

    skipped_reasons: dict[str, int] = {}
    for item in result.items:
        if item.skipped_reason:
            skipped_reasons[item.skipped_reason.split(".")[0]] = (
                skipped_reasons.get(item.skipped_reason.split(".")[0], 0) + 1
            )
    for reason, count in sorted(skipped_reasons.items(), key=lambda kv: -kv[1]):
        print(f"      {count:>4}  {reason}")

    if result.failed:
        print(f"\n  {len(result.failed)} FAILED:")
        for item in result.failed[:10]:
            print(f"      {item.storage_key}: {item.error}")
        return 1

    if result.dry_run:
        print("\n  Re-run with --confirm to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
