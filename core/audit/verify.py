# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Audit chain verifier.

    python -m core.audit.verify
    python -m core.audit.verify --prefix audit/2026/08/

Exits non-zero if the chain is broken, so it can be wired into monitoring
or a nightly job. A broken chain is a security finding, not a bug report:
follow runbooks/RUNBOOK_INCIDENT_RESPONSE.md.

FOUND AND FIXED (2026-08-17 audit, MEDIUM): this tool hardcoded a direct
`from core.audit.sink import S3AuditSink` construction and refused to
run at all on GCP/Azure ("Audit verification is only implemented for
AWS"; exit 2), even though core/storage/factory.py's build_audit_sink()
already had full, working GCP/Azure support by the time of this audit -
AzureBlobAuditSink and GCSAuditSink both implement the same
__call__/last_hash/read_all interface S3AuditSink does (see
core/audit/sink.py), and both schedulers had already been fixed to
route through build_audit_sink() rather than hardcoding S3AuditSink
directly (see that function's own docstring for that earlier fix). This
tool alone never got the same treatment - proven live: constructing
Settings with a cloud provider of gcp or azure and calling main()
printed the AWS-only message and returned 2 immediately, without ever
attempting to read a single audit event, on a codebase that could
already read them on both clouds. Nightly audit chain verification -
the control this tool exists to automate, and the one place
runbooks/RUNBOOK_INCIDENT_RESPONSE.md and RUNBOOK_INSTALL.md point
operators to for confirming chain integrity - was silently impossible
on 2 of the 3 supported clouds. Fixed by routing through
build_audit_sink() like everything else already does.

FOUND AND FIXED (2026-08-17 audit, MEDIUM, "audit chain forks under
concurrent writers"): both a full read and a `--prefix` partial read
previously walked events as one strict linear sequence (this file's own
now-removed `_verify_partial`, and core.audit.log.AuditLog.verify_chain
before its fix), which reports CHAIN BROKEN the moment the incremental
scheduler and the bulk-export scheduler (or two replicas of either) both
resume from the same chain tip and append near-simultaneously - a
legitimate fork, not tampering. See core/audit/log.py's
AuditLog.diagnose_chain() docstring for the full reasoning and the
local proof. This tool now calls diagnose_chain() directly instead of
verify_chain()/`_verify_partial`, so it can also surface fork_points and
root_events to the operator - an alert that turns out to be routine
concurrent writers should visibly look different from one that turns
out to be real tampering, not just print the same bare PASS/FAIL either
way.

This tool never compares an event's `action` to anything: it verifies
every event in a key range regardless of what the event is called. See
the comment on `--prefix` below, which is a storage KEY prefix and not
an action prefix - an easy and consequential thing to confuse.
"""

from __future__ import annotations

import argparse
import sys

from core.audit.log import AuditLog
from core.config.settings import Settings
from core.storage.factory import build_audit_sink

# Display-only prefix per cloud for the "Reading audit events from ..."
# progress line below - purely cosmetic, matches each cloud's own key
# layout terminology (S3 "bucket", Azure "container", GCS "bucket") so
# the message reads naturally regardless of provider; has no effect on
# which sink is actually constructed or how it reads.
_SCHEME_BY_PROVIDER = {"aws": "s3://", "gcp": "gs://", "azure": "azure-blob://"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify audit log chain integrity.")
    parser.add_argument(
        "--prefix",
        default="audit/",
        # NOT AN ACTION PREFIX. This is an object STORAGE KEY prefix -
        # `audit/YYYY/MM/DD/...` per core/audit/sink.py's layout - so it
        # selects a TIME RANGE, not a kind of event. Every event in that
        # range is verified whatever its `action` says.
        help="Restrict verification to a key prefix, e.g. audit/2026/08/. "
             "This is a storage key prefix (a time range), not an action "
             "name filter. Note that verifying a partial range cannot confirm "
             "the chain links correctly to events outside that range.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()

    # build_audit_sink() raises ValueError for any provider it doesn't
    # recognize (see core/storage/factory.py) - Settings.from_env()
    # already restricts cloud_provider to aws/gcp/azure, so that branch
    # is unreachable in practice, but this tool no longer needs its own
    # separate allow-list to stay correct as providers are added there.
    sink = build_audit_sink(settings)

    scheme = _SCHEME_BY_PROVIDER.get(settings.cloud_provider, "")
    print(f"Reading audit events from {scheme}{settings.audit_bucket}/{args.prefix} ...")
    events = sink.read_all(prefix=args.prefix)

    if not events:
        print("No audit events found. If this platform has been used, that is itself a finding.")
        return 1

    # A full read (the default `audit/` prefix) is `closed=True`: every
    # event's parent should genuinely be present in the set, so an
    # unresolved prev_hash is a real finding. A `--prefix` partial read
    # is `closed=False`: it begins mid-chain by construction, so the
    # events at its own boundary are expected to reference a parent
    # outside the queried range - see AuditLog.diagnose_chain()'s
    # docstring for why that split exists and what each mode still does
    # and doesn't catch.
    partial = args.prefix != "audit/"
    diag = AuditLog.diagnose_chain(events, closed=not partial)

    print(f"Events checked: {len(events)}")
    print(f"First: {events[0]['timestamp']}")
    print(f"Last:  {events[-1]['timestamp']}")

    if diag.fork_points:
        print(
            f"Note: {diag.fork_points} fork point(s) detected "
            f"({diag.root_events} root event(s) total) - expected when the "
            "incremental scheduler, the bulk-export scheduler, or replicas "
            "of either resume from the same chain tip and write near-"
            "simultaneously. Not a tampering indicator on its own; see "
            "core/audit/log.py's AuditLog.diagnose_chain() docstring."
        )

    if diag.ok:
        print("RESULT: chain intact.")
        return 0

    print("RESULT: CHAIN BROKEN - possible tampering or deletion.")
    if diag.corrupted_event_hashes:
        print(f"  {len(diag.corrupted_event_hashes)} event(s) with a hash that doesn't match their own content.")
    if diag.unresolved_prev_hashes:
        print(
            f"  {len(diag.unresolved_prev_hashes)} event(s) whose prev_hash names a record "
            "not present in this read - a deleted or missing predecessor."
        )
    print("Do not 'fix' this by regenerating the log. Follow")
    print("runbooks/RUNBOOK_INCIDENT_RESPONSE.md and preserve the current state.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
