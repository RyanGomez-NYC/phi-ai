# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Run the EMR emulators.

    python -m emulators                    # every vendor in VENDORS, on DEFAULT_PORTS
    python -m emulators --vendor cerner    # just one
    python -m emulators --record-url http://127.0.0.1:8080/smart/launch
    python -m emulators --client-jwks ./jwks.json --client-secret cid:secret

ALL DATA IS SYNTHETIC. These stand in for real EMRs so the integration can
be exercised end to end without one - which is the only way to test
vendors whose registration processes take weeks each and whose sandboxes
are not always obtainable. Which vendors, and on which ports, is read
from emulators/vendors.py (VENDORS and DEFAULT_PORTS), never listed here.

WHAT A GREEN RUN HERE PROVES, and what it does not. It proves the client
handles the shapes these servers produce: the auth flows, pagination, the
async bulk export handshake, capability-gated writes, conditional create,
and in-context launch with a really-signed id_token. That is the majority
of integration defects. It does NOT prove a particular customer's build
behaves identically - every profile still says to confirm against the
instance's own CapabilityStatement, and that stands.
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Optional

import json

from emulators.server import serve
from emulators.vendors import DEFAULT_PORTS, VENDORS


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m emulators",
        description="Run synthetic EMR emulators for "
                    + ", ".join(VENDORS[key].name for key in sorted(VENDORS))
                    + f" (ports {min(DEFAULT_PORTS.values())}-{max(DEFAULT_PORTS.values())}).",
    )
    parser.add_argument("--vendor", choices=sorted(VENDORS),
                        help="run one vendor instead of every vendor in VENDORS")
    parser.add_argument("--record-url",
                        default="http://127.0.0.1:8080/smart/launch",
                        help="this platform's SMART launch endpoint, for the "
                             "in-context launch page")
    parser.add_argument("--port", type=int, help="override the port (single vendor only)")
    parser.add_argument("--client-jwks", metavar="PATH",
                        help="the client's PUBLIC JWK Set (RFC 7517 JSON) to register with "
                             "every emulator, so token endpoints verify client_assertion "
                             "signatures the way a vendor holding the registered key does. "
                             "Without it signatures are NOT verified (a WARNING says so); "
                             "alg, audience, expiry and claims still are")
    parser.add_argument("--client-secret", metavar="CLIENT_ID:SECRET", action="append",
                        default=[],
                        help="a client_id:secret pair to register with the emulators that "
                             "honour a client secret; repeatable. Without any, a secret "
                             "is accepted unverified (a WARNING says so)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client_jwks = None
    if args.client_jwks:
        with open(args.client_jwks, "r", encoding="utf-8") as handle:
            client_jwks = json.load(handle)
        if not isinstance(client_jwks, dict) or not isinstance(client_jwks.get("keys"), list):
            parser.error(f"--client-jwks {args.client_jwks}: not a JWK Set ({{'keys': [...]}})")
        if any("d" in k for k in client_jwks["keys"]):
            # A JWK with `d` is a PRIVATE key; the emulator must hold only
            # the public half, as a vendor would.
            parser.error(f"--client-jwks {args.client_jwks}: a key carries the private "
                         "parameter 'd'; register the PUBLIC JWK Set only")
    client_credentials = {}
    for pair in args.client_secret:
        if ":" not in pair:
            parser.error(f"--client-secret {pair!r}: expected CLIENT_ID:SECRET")
        client_id, secret = pair.split(":", 1)
        client_credentials[client_id] = secret

    vendors = [args.vendor] if args.vendor else sorted(VENDORS)
    for key in vendors:
        port = args.port if (args.port and args.vendor) else DEFAULT_PORTS[key]
        serve(key, port, args.record_url, client_jwks=client_jwks,
              client_credentials=client_credentials or None)

    print(f"\n{len(vendors)} emulator(s) running. All data is SYNTHETIC.\n")
    for key in vendors:
        port = args.port if (args.port and args.vendor) else DEFAULT_PORTS[key]
        vendor = VENDORS[key]
        print(f"  {vendor.name:26} http://127.0.0.1:{port}{vendor.fhir_path}")
        print(f"  {'':26} launch page: http://127.0.0.1:{port}/emulator/launch")
        traits = []
        # Derived from the accept flags, so a vendor that honours both
        # grants is not misreported as secret-only.
        grants = [label for label, accepted in (
            ("JWT assertion", vendor.accepts_jwt_assertion),
            ("client_secret", vendor.accepts_client_secret),
        ) if accepted]
        traits.append(" or ".join(grants) or "no client_credentials grant")
        if vendor.accepts_jwt_assertion:
            traits.append(f"assertion alg: {'/'.join(vendor.assertion_algorithms)}")
        traits.append("$export" if vendor.supports_bulk_export else "NO $export")
        if vendor.supports_conditional_create:
            traits.append("conditional create")
        traits.append(f"creatable: {', '.join(vendor.creatable) or 'nothing'}")
        print(f"  {'':26} {' · '.join(traits)}\n")

    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
