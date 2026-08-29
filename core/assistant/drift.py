# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Model drift monitoring: a fixed probe suite, run on a schedule.

    python -m core.assistant.drift                      # run + report
    python -m core.assistant.drift --probes config/assistant_probes.yaml

WHAT "DRIFT" HONESTLY MEANS HERE. The assistant's model is operated by a
provider, and its behaviour can change under this deployment's feet: a
pinned id can be re-served, a default id can move, and the same id can
answer differently after provider-side changes. This module makes that
observable the only way it can be observed - by asking the SAME
questions with the SAME expectations on a schedule and recording whether
the answers still satisfy them. It measures behaviour against
expectations; it does not (and cannot) measure internal model change.

PROBES ARE EXPECTATIONS, NOT TRANSCRIPTS. Each probe states a question
and checks that are stable across phrasings: whether the answer was
refused, which documentation it cited, which tools it used, and which
key phrases it must (or must never) contain. Asserting exact wording
would fail on every temperature wobble and teach operators to ignore
the suite - the same reasoning RELEASE_CHECKLIST.md gives for scanners
tuned until they find nothing, inverted.

RESULTS LAND IN TELEMETRY (kind='drift_probe', one row per probe) when
the aiops role is configured, so the ops page can put pass rates next
to the models-seen table - a pass-rate change that coincides with a
model change IS the drift signal. Without telemetry the run still
prints and exits nonzero on failure, so a cron job still alerts.

PROBES CONTAIN NO PHI, BY CONSTRUCTION AND BY CHECK: the runner builds a
documentation-only session (no clinical, analytics or research access,
capabilities=frozenset()), so a probe that tried to read a record would
fail with an unknown tool rather than read anything. Drift runs are
audited like every other question when a sink is available.

The committed config/assistant_probes.example.yaml covers the questions
whose answers this project most needs to stay right - the compliance
posture ones. Copy it to config/assistant_probes.yaml (gitignored, like
every deployer-owned config) and extend it with probes for your own
deployment's sore spots.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("phi-ai.assistant.drift")

DEFAULT_PROBES_PATH = "config/assistant_probes.yaml"
EXAMPLE_PROBES_PATH = "config/assistant_probes.example.yaml"


class ProbeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Probe:
    name: str
    question: str
    expect_refused: bool = False
    answer_must_contain_any: tuple[str, ...] = ()
    answer_must_not_contain: tuple[str, ...] = ()
    must_cite_any: tuple[str, ...] = ()
    must_use_tool: Optional[str] = None


@dataclass
class ProbeResult:
    probe: Probe
    passed: bool
    failures: list[str] = field(default_factory=list)
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tools_used: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        return "; ".join(self.failures)


def load_probes(path: str) -> list[Probe]:
    import yaml

    file = Path(path)
    if not file.is_file():
        raise ProbeConfigError(
            f"{path} does not exist. Copy {EXAMPLE_PROBES_PATH} to "
            f"{DEFAULT_PROBES_PATH} and edit it - see this module's docstring."
        )
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    entries = raw.get("probes")
    if not isinstance(entries, list) or not entries:
        raise ProbeConfigError(f"{path} holds no 'probes' list.")

    probes: list[Probe] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("question"):
            raise ProbeConfigError(f"{path}: probe {i} needs at least 'name' and 'question'.")
        name = str(entry["name"])
        if name in seen:
            raise ProbeConfigError(f"{path}: duplicate probe name {name!r} - results would merge.")
        seen.add(name)
        expect = entry.get("expect") or {}
        unknown = set(expect) - {
            "refused", "answer_must_contain_any", "answer_must_not_contain",
            "must_cite_any", "must_use_tool",
        }
        if unknown:
            # Fail rather than default: a misspelled expectation that
            # silently checks nothing is a probe that always passes,
            # which is worse than no probe (the SMART issuer loader's
            # known-gap comment, learned from).
            raise ProbeConfigError(
                f"{path}: probe {name!r} has unknown expectation(s): {sorted(unknown)}"
            )
        probes.append(Probe(
            name=name,
            question=str(entry["question"]),
            expect_refused=bool(expect.get("refused", False)),
            answer_must_contain_any=tuple(expect.get("answer_must_contain_any") or ()),
            answer_must_not_contain=tuple(expect.get("answer_must_not_contain") or ()),
            must_cite_any=tuple(expect.get("must_cite_any") or ()),
            must_use_tool=expect.get("must_use_tool"),
        ))
    return probes


def evaluate(probe: Probe, reply) -> ProbeResult:
    """Check one reply against one probe's expectations. Pure."""
    failures: list[str] = []
    text_lower = (reply.text or "").lower()

    if bool(reply.refused) != probe.expect_refused:
        failures.append(
            f"expected refused={probe.expect_refused}, got {bool(reply.refused)}"
        )
    if probe.answer_must_contain_any and not any(
        phrase.lower() in text_lower for phrase in probe.answer_must_contain_any
    ):
        failures.append(
            f"answer contains none of {list(probe.answer_must_contain_any)}"
        )
    for phrase in probe.answer_must_not_contain:
        if phrase.lower() in text_lower:
            failures.append(f"answer contains forbidden phrase {phrase!r}")
    if probe.must_cite_any and not any(
        any(cited.startswith(want) for cited in (reply.sources or []))
        for want in probe.must_cite_any
    ):
        failures.append(
            f"cited none of {list(probe.must_cite_any)} (cited: {reply.sources or []})"
        )
    if probe.must_use_tool and probe.must_use_tool not in (reply.tools_used or []):
        failures.append(
            f"did not use {probe.must_use_tool} (used: {list(reply.tools_used or [])})"
        )

    return ProbeResult(
        probe=probe,
        passed=not failures,
        failures=failures,
        input_tokens=getattr(reply, "input_tokens", 0),
        output_tokens=getattr(reply, "output_tokens", 0),
        tools_used=tuple(reply.tools_used or ()),
    )


def run_probes(runtime, probes: list[Probe], audit=None) -> list[ProbeResult]:
    """One fresh, documentation-only session per probe, so probes cannot
    contaminate each other's context and a drift run exercises the same
    cold path a user's first question does."""
    results: list[ProbeResult] = []
    for probe in probes:
        session = runtime.session_for(
            actor="drift-probe",
            capabilities=frozenset(),   # documentation tools only
            audit=audit,
            require_audit=audit is not None,
        )
        started = time.monotonic()
        reply = session.ask(probe.question)
        result = evaluate(probe, reply)
        result.latency_ms = int((time.monotonic() - started) * 1000)
        results.append(result)
    return results


def record_results(connection_factory, results: list[ProbeResult],
                   provider: str, model: str) -> None:
    from core.assistant import telemetry

    for result in results:
        telemetry.record_interaction(
            connection_factory,
            kind="drift_probe",
            username="drift-probe",
            provider=provider,
            model=model,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tool_calls=len(result.tools_used),
            tools_used=",".join(result.tools_used),
            probe_name=result.probe.name,
            probe_passed=result.passed,
            probe_detail=result.detail or None,
        )


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Run the assistant drift-probe suite.")
    parser.add_argument("--probes", default=DEFAULT_PROBES_PATH,
                        help=f"probe file (default {DEFAULT_PROBES_PATH}; the committed "
                             f"{EXAMPLE_PROBES_PATH} is the template)")
    args = parser.parse_args()

    from core.assistant import runtime as assistant_runtime
    from core.assistant.config import AssistantConfigError, settings_from_env

    try:
        probes = load_probes(args.probes)
    except ProbeConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        assistant_settings = settings_from_env()
    except AssistantConfigError as exc:
        print(f"Assistant configuration problem:\n\n{exc}", file=sys.stderr)
        return 2
    if assistant_settings is None:
        print("PHI_AI_ASSISTANT_ENABLED is not set - nothing to probe.", file=sys.stderr)
        return 2

    rt = assistant_runtime.build(assistant_settings=assistant_settings)

    print(f"Running {len(probes)} probe(s) against {assistant_settings.describe()}\n")
    results = run_probes(rt, probes)

    # Telemetry, when the deployment has it - same graceful skip as the
    # web worker's own recording.
    ops_connection = None
    try:
        from core.config.settings import Settings
        from core.db.connection import connect

        platform = Settings.from_env()
        if platform.assistant_ops_configured():
            ops_connection = lambda: connect(  # noqa: E731
                platform, platform.assistant_ops_username
            )
    except Exception as exc:
        log.warning("platform settings unavailable, drift results not recorded: %s", exc)

    record_results(ops_connection, results,
                   provider=assistant_settings.provider,
                   model=assistant_settings.resolved_model)

    failed = [r for r in results if not r.passed]
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"  {mark}  {result.probe.name}  ({result.latency_ms} ms)")
        for failure in result.failures:
            print(f"        - {failure}")

    print(f"\n{len(results) - len(failed)}/{len(results)} probes passed.")
    if ops_connection is None:
        print("(Not recorded: PHI_AI_ASSISTANT_OPS_USERNAME is unset - the ops "
              "page will not show this run.)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
# Made by Ryan Gomez & Co. Inc.
