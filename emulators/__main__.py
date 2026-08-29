# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Run the EMR emulators.

    python -m emulators                    # all five
    python -m emulators --vendor cerner    # just one
    python -m emulators --record-url http://127.0.0.1:8080/smart/launch

ALL DATA IS SYNTHETIC. These stand in for real EMRs so the integration can
be exercised end to end without one - which is the only way to test five
vendors whose registration processes take weeks each and whose sandboxes
are not always obtainable.

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

from emulators.server import serve
from emulators.vendors import DEFAULT_PORTS, VENDORS


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m emulators",
        description="Run synthetic EMR emulators for Epic, Cerner, athenahealth, "
                    "eClinicalWorks and NextGen.",
    )
    parser.add_argument("--vendor", choices=sorted(VENDORS),
                        help="run one vendor instead of all five")
    parser.add_argument("--record-url",
                        default="http://127.0.0.1:8080/smart/launch",
                        help="this platform's SMART launch endpoint, for the "
                             "in-context launch page")
    parser.add_argument("--port", type=int, help="override the port (single vendor only)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    vendors = [args.vendor] if args.vendor else sorted(VENDORS)
    for key in vendors:
        port = args.port if (args.port and args.vendor) else DEFAULT_PORTS[key]
        serve(key, port, args.record_url)

    print("\nEmulators running. All data is SYNTHETIC.\n")
    for key in vendors:
        port = args.port if (args.port and args.vendor) else DEFAULT_PORTS[key]
        vendor = VENDORS[key]
        print(f"  {vendor.name:26} http://127.0.0.1:{port}{vendor.fhir_path}")
        print(f"  {'':26} launch page: http://127.0.0.1:{port}/emulator/launch")
        traits = []
        traits.append("client_secret" if vendor.accepts_client_secret else "JWT assertion")
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
