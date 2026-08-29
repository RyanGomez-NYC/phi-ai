# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Talk to the assistant from a terminal: `python -m core.assistant`

    python -m core.assistant                    # interactive
    python -m core.assistant --ask "..."        # one question, for scripts
    python -m core.assistant --check            # verify it can reach the model

USABLE BEFORE ANYTHING IS DEPLOYED, which is most of the point. An
operator at the start of runbooks/RUNBOOK_AWS_SETUP.md has no bucket, no
database and no audit sink, and that is exactly when the questions are
hardest. So a missing platform configuration degrades this to a
documentation-only assistant with a stated reason, rather than refusing
to start - the same graceful-skip posture the rest of this project uses
for optional infrastructure.

AUDITING IN THE CLI, STATED HONESTLY. When an audit sink is reachable,
every question is recorded exactly as the web interface records one. When
it is not - during installation, against no infrastructure - the session
runs unaudited and says so on startup. That is defensible only because
whoever can run this command already has the deployment's environment and
credentials in their shell; it would not be defensible for a web user,
and core/web/app.py accordingly does not allow it.
"""

from __future__ import annotations

import argparse
import logging
import sys

from core.assistant import runtime
from core.assistant.config import AssistantConfigError, assistant_enabled, settings_from_env
from core.assistant.provider import AssistantUnavailable

BANNER = """\
PHI AI Platform assistant
  Ask about installing, operating or using this platform. Type 'exit' to leave.
  See runbooks/RUNBOOK_AI_ASSISTANT.md.
"""


def _load_platform_context():
    """(settings, profile, reader, audit, note) - any of which may be None."""
    try:
        from core.config.scale_profile import profile_from_env
        from core.config.settings import ConfigError, Settings
    except ImportError as exc:  # pragma: no cover - import guard
        return None, None, None, None, f"could not import platform configuration: {exc}"

    try:
        settings = Settings.from_env()
        profile = profile_from_env()
    except (ConfigError, ValueError) as exc:
        return (
            None,
            None,
            None,
            None,
            f"no platform configuration found ({exc}). Answering from documentation "
            "only - which is the expected state before the infrastructure exists.",
        )

    reader = audit = None
    notes = []
    try:
        from core.audit.log import AuditLog
        from core.crypto.envelope import EnvelopeEncryptor
        from core.storage.factory import build_audit_sink, build_kms, build_storage

        storage = build_storage(settings)
        sink = build_audit_sink(settings)
        audit = AuditLog(sink=sink, last_known_hash=sink.last_hash())

        if settings.db_target_configured() and settings.db_reader_username:
            from core.db.connection import connect
            from core.web.data import LiveRecordReader

            reader = LiveRecordReader(
                connection_factory=lambda: connect(settings, settings.db_reader_username),
                storage=storage,
                encryptor=EnvelopeEncryptor(kms=build_kms(settings)),
                audit_sink=sink,
            )
        else:
            notes.append(
                "the Postgres index is not configured, so holdings and retention "
                "questions cannot be answered from live data"
            )
    except Exception as exc:
        notes.append(f"could not reach the object store or audit log ({exc})")

    return settings, profile, reader, audit, "; ".join(notes) or None


def _print_reply(reply) -> None:
    print()
    print(reply.text)
    if reply.sources:
        print()
        print("  sources: " + ", ".join(reply.sources))
    if reply.truncated:
        print("  (answer was cut short - ask something narrower)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m core.assistant")
    parser.add_argument("--ask", help="ask one question and exit")
    parser.add_argument(
        "--check", action="store_true", help="verify the model is reachable, then exit"
    )
    parser.add_argument(
        "--purpose",
        help="purpose of use recorded against any record the assistant reads "
        "(treatment, payment, operations, patient_request, legal). Only meaningful "
        "at PHI_AI_ASSISTANT_PHI_ACCESS=lookup; without it no record is read.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    if not assistant_enabled():
        print(
            "The assistant is not enabled. It is an optional add-on and off by "
            "default.\n\nSet PHI_AI_ASSISTANT_ENABLED=true and read "
            "runbooks/RUNBOOK_AI_ASSISTANT.md first - enabling it opens a network "
            "path from this deployment to a language model, and which provider you "
            "choose determines whether that traffic leaves your cloud account.",
            file=sys.stderr,
        )
        return 1

    try:
        assistant_settings = settings_from_env()
    except AssistantConfigError as exc:
        print(f"Assistant configuration problem:\n\n{exc}", file=sys.stderr)
        return 1

    platform_settings, profile, reader, audit, note = _load_platform_context()

    try:
        rt = runtime.build(
            assistant_settings=assistant_settings,
            platform_settings=platform_settings,
            profile=profile,
            reader=reader,
        )
    except AssistantUnavailable as exc:
        print(f"Assistant unavailable: {exc}", file=sys.stderr)
        return 1

    actor = _operator_name()
    clinical, clinical_note = _cli_clinical_access(rt, actor, audit, args.purpose)

    session = rt.session_for(
        actor=actor,
        audit=audit,
        # Unaudited only when there is genuinely no sink to write to.
        require_audit=audit is not None,
        clinical=clinical,
    )

    if args.check:
        reply = session.ask("Reply with the words: assistant reachable.")
        print(reply.text)
        return 0 if not reply.refused else 1

    if args.ask:
        _print_reply(session.ask(args.ask))
        return 0

    print(BANNER)
    print(f"  model: {assistant_settings.describe()}")
    if not assistant_settings.reads_clinical_content:
        print("  PHI:   no access to clinical content - it cannot read any record.")
    if not assistant_settings.stays_in_org_cloud:
        print("  note:  requests go outside this deployment's cloud account.")
    if assistant_settings.reads_clinical_content:
        print(f"  PHI:   access tier '{assistant_settings.phi_access}' is enabled.")
    if clinical is not None:
        print(
            f"  PHI:   records ARE readable in this session, audited as disclosures "
            f"with purpose '{clinical.purpose}'."
        )
    elif clinical_note:
        print(f"  PHI:   no records will be read - {clinical_note}.")
    if audit is None:
        print("  note:  no audit sink is reachable, so this session is NOT recorded.")
    if note:
        print(f"  note:  {note}")
    if rt.knowledge_base.is_empty():
        print(
            "  note:  no documentation was found, so answers will not be grounded in "
            "this project's runbooks. Run from the repository root."
        )
    print()

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in ("exit", "quit", ":q"):
            return 0
        if not question:
            continue
        try:
            _print_reply(session.ask(question))
        except RuntimeError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 1


def _cli_clinical_access(rt, actor: str, audit, purpose: str | None):
    """Clinical tool access for a terminal session, or (None, reason).

    The `in_context` tier is meaningless here and is refused rather than
    quietly downgraded: that tier means "the record on the user's screen",
    and a terminal has no screen. An operator who wants record access from
    the CLI has to be on `lookup`, which is the tier that was actually
    reviewed for it.

    `lookup` additionally requires a purpose of use AND an audit sink.
    Unlike documentation questions, a clinical read may not proceed
    unaudited under any circumstances - the CLI's own tolerance for
    running without a sink (see this module's docstring) stops here.
    """
    from core.assistant.config import PHI_ACCESS_LOOKUP
    from core.assistant.tools import ClinicalAccess

    # Named for what it is: this is the ASSISTANT's configuration, not the
    # platform's Settings dataclass. The two are different objects and the
    # bare name `settings` for either is how attribute typos hide.
    assistant_settings = rt.settings
    if not assistant_settings.reads_clinical_content:
        return None, None
    if assistant_settings.phi_access != PHI_ACCESS_LOOKUP:
        return None, (
            "PHI_AI_ASSISTANT_PHI_ACCESS=in_context has no meaning in a terminal "
            "session - it means 'the record open on screen'. Records are not readable here"
        )
    if rt.reader is None:
        return None, "the Postgres index is not configured, so records cannot be read"
    if audit is None:
        return None, (
            "no audit sink is reachable, and a clinical read is never made without one"
        )

    from core.web.auth import NotAuthorized, validate_purpose

    try:
        resolved = validate_purpose(purpose)
    except NotAuthorized:
        from core.web.auth import PURPOSES_OF_USE

        return None, (
            "no --purpose was given, so records will not be read. Valid values: "
            + ", ".join(code for code, _ in PURPOSES_OF_USE)
        )

    def record_read(action: str, resource_key: str) -> None:
        audit.record(
            actor=actor, action=action, resource_key=resource_key, purpose_of_use=resolved
        )

    return (
        ClinicalAccess(
            reader=rt.reader,
            record_read=record_read,
            purpose=resolved,
            tier=assistant_settings.phi_access,
        ),
        None,
    )


def _operator_name() -> str:
    import getpass

    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli:unknown"


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
