# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Run verification on a schedule, and record that it happened.

    python -m core.verify.scheduled

WHY THIS EXISTS. A hash-chained audit log nobody verifies is just a log.
The chain makes tampering DETECTABLE; it does not make it DETECTED. The
same is true of every other check in this framework - they establish
capability, and capability that is never exercised is indistinguishable
from its absence when something actually goes wrong. This is the thing
that exercises them.

TWO OUTPUTS, and the second is the point.

  1. The rendered report, to logs, for a human or a log aggregator.
  2. AN AUDIT ENTRY PER RUN, into the same hash-chained log this platform
     uses for PHI access.

The second matters because "we verify our records regularly" is a claim
someone will eventually be asked to substantiate - during an audit, a
breach investigation, or a dispute about whether a record was intact at a
given date. A log line is not evidence; it can be edited or rotated away.
An entry in the hash-chained audit log is, and it inherits every property
that log already has. Recording verification into the evidence store the
platform already maintains is nearly free and turns an operational habit
into something demonstrable.

The entry records the OUTCOME and the counts, never the findings' detail:
resource identifiers belong in the report, not in an append-only log that
is deliberately hard to prune.

WHAT IT DELIBERATELY DOES NOT DO: page anyone. Alerting belongs to
whatever the deploying organisation already runs. This exits non-zero and
logs at ERROR on critical findings, which every supervisor, cron wrapper
and container platform already knows how to act on. Building a second
notification path would mean credentials, delivery guarantees and an
on-call model this project has no business owning.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core.config.settings import env_var

log = logging.getLogger("phi-ai.verify.scheduled")

# Daily by default. Verification is not free - it re-reads objects and
# talks to the source EMR - and the failures it looks for do not appear
# minute to minute. A daily cadence bounds how long a problem can sit
# undiscovered to something defensible without making the check itself a
# load problem.
DEFAULT_INTERVAL_SECONDS = 86_400

# Deep verification is expensive enough that running it on every cycle is
# not realistic, and cheap enough weekly that skipping it entirely is not
# justifiable. Zero disables.
DEFAULT_DEEP_EVERY = 7


def record_run(audit, report, deep: bool) -> None:
    """Write one audit entry describing the run's outcome.

    Never raises past the caller: a verification run that FOUND something
    must still report it even if recording the run failed, and the
    rendered report has already been logged by then.
    """
    if audit is None:
        log.warning(
            "no audit sink configured - this verification run leaves no durable evidence "
            "that it happened"
        )
        return

    summary = (
        f"outcome={report.worst.value} "
        f"flows={len(report.flows)} "
        f"critical={len(report.critical)} "
        f"unchecked={len(report.skipped)} "
        f"depth={'deep' if deep else 'sample'}"
    )
    try:
        audit.record(
            actor="phi-ai-verification",
            action="record.verify",
            resource_key=summary,
            purpose_of_use="operations",
        )
    except Exception as exc:
        log.error("could not record the verification run in the audit log: %s", exc)


def run_once(deep: bool, export_dir: Optional[str], skip_source: bool) -> int:
    """One verification pass: build the report, render it, record the run."""
    from core.audit.log import AuditLog
    from core.config.settings import Settings
    from core.storage.factory import build_audit_sink
    from core.verify.__main__ import build_verification_report

    # Builds the report through the SAME function the CLI uses, so a
    # scheduled run and a hand-run check can never disagree about what
    # "verified" means.
    report = build_verification_report(
        deep=deep, export_dir=export_dir, skip_source=skip_source
    )
    print(report.render())

    audit = None
    try:
        settings = Settings.from_env()
        sink = build_audit_sink(settings)
        audit = AuditLog(sink=sink, last_known_hash=sink.last_hash())
    except Exception as exc:
        log.error("could not open the audit log to record this run: %s", exc)

    record_run(audit, report, deep)
    return report.exit_code()


def main(argv: Optional[list] = None) -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    parser = argparse.ArgumentParser(
        prog="python -m core.verify.scheduled",
        description="Run PHI AI Platform verification on an interval and record each run.",
    )
    # EVERY default below reads through env_var(), never os.environ.get().
    # These are all defaulted reads, so a missed variable does not raise -
    # it silently substitutes a value the operator did not choose. That
    # matters most for --skip-source: an operator who set it because the
    # source EMR has been decommissioned would otherwise get the default
    # (False) and a verification run that keeps trying to compare against
    # a system that no longer exists, reported as an unchecked flow with
    # no indication the setting was ignored.
    parser.add_argument(
        "--interval-seconds", type=int,
        default=int(
            env_var("VERIFY_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS))
            or DEFAULT_INTERVAL_SECONDS
        ),
    )
    parser.add_argument(
        "--deep-every", type=int,
        default=int(
            env_var("VERIFY_DEEP_EVERY", str(DEFAULT_DEEP_EVERY)) or DEFAULT_DEEP_EVERY
        ),
        help="run a deep pass every Nth cycle (0 disables deep passes entirely)",
    )
    parser.add_argument("--export-dir", default=env_var("VERIFY_EXPORT_DIR"))
    parser.add_argument(
        "--skip-source", action="store_true",
        default=(env_var("VERIFY_SKIP_SOURCE", "") or "").lower()
        in ("1", "true", "yes"),
        help="skip the EMR comparison. Set this ONLY once the source EMR is "
             "decommissioned - while it exists, this is the check that expires.",
    )
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = parser.parse_args(argv)

    if args.skip_source:
        log.warning(
            "source EMR comparison is DISABLED. If the source still exists, this is the "
            "one check that cannot be run later - see RUNBOOK_VERIFICATION.md."
        )

    if args.once:
        return run_once(args.deep_every == 1, args.export_dir, args.skip_source)

    log.info(
        "verification scheduler started; interval=%ds deep_every=%s",
        args.interval_seconds,
        args.deep_every or "never",
    )

    cycle = 0
    while True:
        cycle += 1
        deep = bool(args.deep_every) and cycle % args.deep_every == 0

        try:
            code = run_once(deep, args.export_dir, args.skip_source)
            if code == 2:
                # ERROR so that whatever already watches this container's
                # logs reacts, without this project owning an alerting
                # path of its own.
                log.error(
                    "VERIFICATION FOUND CRITICAL PROBLEMS - stored data may be at risk. "
                    "See the report above and RUNBOOK_VERIFICATION.md."
                )
            elif code == 1:
                log.warning("verification completed with warnings or unchecked flows")
            else:
                log.info("verification clean")
        except Exception:
            # A crash must not end the loop: the next cycle may well
            # succeed, and a scheduler that dies on one bad run stops
            # verifying silently, which is the failure this whole module
            # exists to prevent.
            log.exception("verification cycle failed; continuing to the next interval")

        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
