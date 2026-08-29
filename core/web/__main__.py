# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Run the web interface: `python -m core.web`

Binds to LOCALHOST by default, deliberately. In its recommended
deployment this application does not authenticate users - it trusts an
identity established by a reverse proxy in front of it (see
core/web/auth.py) - so a default of 0.0.0.0 would turn a missing proxy
from a misconfiguration into an open PHI endpoint. Binding elsewhere is
possible and must be explicit.

That default matters slightly less, and not at all differently, for a
deployment using local accounts (PHI_AI_WEB_LOCAL_ACCOUNTS - see
core/web/local_auth.py): there the application does verify a credential,
so an exposed port is a login page rather than an open door. It is still
a login page in front of PHI, reachable by anyone who can route to it,
and it still belongs behind TLS termination on an interface the
organisation meant to expose.

EVERY VARIABLE BELOW IS READ THROUGH core/config/settings.py's env_var(),
not os.environ.get(). Nearly every read here is defaulted, so a read that
misses the operator's variable does not error - the interface comes up
bound to an interface nobody chose, with an ephemeral session secret that
signs everyone out on restart, and says nothing about it.
"""

from __future__ import annotations

import logging
import sys

from core.config.settings import env_var


def build() -> "FastAPI":  # noqa: F821
    from core.audit.log import AuditLog
    from core.config.settings import Settings
    from core.crypto.envelope import EnvelopeEncryptor
    from core.db.connection import connect
    from core.fhir.client import FHIRIngestionClient
    from core.fhir.documents import DocumentIngestor

    from core.config.scale_profile import profile_from_env
    from core.fhir.emr_profiles import EPIC
    from core.ocr.tesseract import TesseractOCR
    from core.storage.factory import build_audit_sink, build_kms, build_storage
    from core.web.app import create_app
    from core.web.data import LiveRecordReader

    settings = Settings.from_env()
    storage = build_storage(settings)
    encryptor = EnvelopeEncryptor(kms=build_kms(settings))
    audit_sink = build_audit_sink(settings)
    audit = AuditLog(sink=audit_sink, last_known_hash=audit_sink.last_hash())

    # FOUND AND FIXED: this and the ROI service below both connected as
    # `settings.db_username`, which is not a field on Settings and never
    # has been - the dataclass has db_ingest_username and
    # db_reader_username, deliberately separate since
    # core/db/bootstrap_aws.sql grants them different privileges. Every
    # start of `python -m core.web` therefore died with AttributeError
    # before serving a single request, and the same typo broke
    # `python -m core.verify` and `python -m core.fhir.delivery`. Not
    # caught by the test suite because every web test injects a fake
    # reader into create_app() and so never executes build() - the
    # dependency injection that makes the routes testable is exactly
    # what left their wiring untested. tests/test_entrypoints.py now
    # checks every settings attribute these modules read against the
    # real dataclass, which catches the whole class rather than this
    # instance of it.
    if not settings.db_reader_username:
        raise RuntimeError(
            "PHI_AI_DB_READER_USERNAME is not set. The web interface reads the "
            "Postgres index to search patients and list records, so it cannot start "
            "without it. It is in the env_fragment your cloud's Terraform emits - see "
            "runbooks/RUNBOOK_WEB_UI.md."
        )

    reader = LiveRecordReader(
        connection_factory=lambda: connect(settings, settings.db_reader_username),
        storage=storage,
        encryptor=encryptor,
        audit_sink=audit_sink,
    )

    # SMART on FHIR in-context launch. Absent issuer file = disabled,
    # which is the safe default for an allowlist. Loaded BEFORE the app so
    # the frame-ancestors policy and cookie SameSite setting are decided
    # at construction rather than patched on afterwards.
    from core.web.smart.config import load_issuers
    from core.web.smart.launch import SMARTLaunchService

    issuers = load_issuers()

    # DICOM imaging (optional). Mounted only when the imaging index has a
    # role configured - see core/config/settings.py's
    # imaging_target_configured(). Set BEFORE create_app() because the
    # DICOMweb routes and the viewer's CORS policy are decided at
    # construction, the same reason SMART issuers are loaded before the
    # app rather than patched on after.
    if settings.imaging_target_configured():
        app_state_imaging = lambda: connect(settings, settings.imaging_db_username)  # noqa: E731
    else:
        app_state_imaging = None

    # Local accounts (optional, off by default) - the deployment shape
    # for an organisation with no identity provider at all. Read
    # core/web/local_auth.py before enabling it; it is the one place this
    # project stores a credential. Built BEFORE create_app() for the same
    # reason as everything above: the sign-in routes and the identity
    # resolution are decided at construction, not patched on after.
    local_accounts = None
    if (env_var("WEB_LOCAL_ACCOUNTS", "") or "").strip().lower() in (
        "1", "true", "yes"
    ):
        from core.web.local_auth import LocalAuthSettings
        from core.web.login_routes import LocalAccounts

        if not settings.db_target_configured():
            # Fail loud rather than start with a login page that cannot
            # verify anything. Note db_target_configured(), not db_host -
            # GCP has no db_host at all, and checking it directly is the
            # exact bug that silently disabled indexing there for every
            # run (see core/config/settings.py).
            raise RuntimeError(
                "PHI_AI_WEB_LOCAL_ACCOUNTS is set but no Postgres target is "
                "configured. Local accounts live in the same database as the record "
                "index - there is nowhere else to keep them. See "
                "runbooks/RUNBOOK_LOCAL_USERS.md."
            )
        if not env_var("WEB_SESSION_SECRET"):
            # create_app() would otherwise generate an ephemeral one and
            # warn. That is tolerable for CSRF tokens; it is not
            # tolerable here, because every user is signed out on every
            # restart and no two replicas agree on a session at all - a
            # login page that logs everybody out at 3am when the
            # container recycles.
            raise RuntimeError(
                "PHI_AI_WEB_LOCAL_ACCOUNTS is set but PHI_AI_WEB_SESSION_SECRET "
                "is not. The session cookie carries the sign-in; without a stable secret "
                "every restart signs everybody out, and two instances behind a load "
                "balancer never agree on a session."
            )

        local_accounts = LocalAccounts(
            # The SAME role the reader uses, deliberately - see
            # core/web/useradmin.py's _connect() and
            # core/db/bootstrap_gcp.sql for the Cloud SQL IAM constraint
            # that makes a dedicated `phi_ai_authn` role
            # non-portable across the three clouds.
            connection_factory=lambda: connect(settings, settings.db_reader_username),
            settings=LocalAuthSettings.from_env(),
        )

    # Prompt history / saved prompts (core/web/prompt_store.py). Same
    # reader role and the same graceful posture as the ROI service: the
    # table lives in the index database, and a deployment without the
    # index simply does not get the feature.
    from core.web.prompt_store import PromptStore

    prompt_store = PromptStore(
        connection_factory=lambda: connect(settings, settings.db_reader_username),
    )

    # Control panel / integration state (configuration and the model
    # registry) - persisted in the index database, same reader role and
    # the same graceful posture as the prompt store.
    from core.web.platform_state import PlatformState

    platform_state = PlatformState(
        connection_factory=lambda: connect(settings, settings.db_reader_username),
    )

    app = create_app(
        reader=reader,
        audit=audit,
        imaging_connection_factory=app_state_imaging,
        local_accounts=local_accounts,
        prompt_store=prompt_store,
        platform_state=platform_state,
        session_secret_key=env_var("WEB_SESSION_SECRET"),
        embedded_issuers=[i for i in issuers if i.embedded],
        secure_cookies=(env_var("WEB_INSECURE_COOKIES", "") or "").lower()
        not in ("1", "true", "yes"),
    )
    if issuers:
        redirect_uri = env_var("WEB_SMART_REDIRECT_URI")
        if not redirect_uri:
            raise RuntimeError(
                "EMR issuers are registered but PHI_AI_WEB_SMART_REDIRECT_URI is not "
                "set. It must exactly match the redirect URI registered with each EMR - a "
                "mismatch is rejected by the authorization server, by design."
            )
        if not env_var("WEB_SESSION_SECRET"):
            raise RuntimeError(
                "EMR issuers are registered but PHI_AI_WEB_SESSION_SECRET is not set. "
                "A completed launch could not be carried across requests, so every page "
                "after the callback would ask the user to authenticate again."
            )
        app.state.smart = SMARTLaunchService(issuers=issuers, redirect_uri=redirect_uri)

    client = FHIRIngestionClient(
        base_url=settings.fhir_base_url,
        profile=EPIC,
        storage=storage,
        encryptor=encryptor,
        audit=audit,
        retention_years=settings.retention_years,
        retention_years_overrides=settings.retention_years_overrides,
        profile_config=profile_from_env(),
    )
    app.state.ingestor = DocumentIngestor(client=client, ocr_engine=TesseractOCR())

    from core.fhir.roi import ROIService

    # Optional AI assistant (core/assistant/). Absent variable = disabled,
    # and disabled is the default: this is the only component that talks
    # to anything outside the deployment.
    #
    # A misconfiguration here REFUSES TO START rather than quietly
    # serving the interface without the assistant, matching how a
    # registered SMART issuer without a redirect URI is handled above. An
    # operator who set PHI_AI_ASSISTANT_ENABLED=true asked for this
    # feature; discovering it silently absent from a running deployment
    # is worse than a startup failure that names the cause.
    from core.assistant import assistant_enabled, settings_from_env

    if assistant_enabled():
        from core.assistant import runtime as assistant_runtime

        # Population analytics and name search, each independently
        # optional and each with its OWN read-only role. Absent
        # configuration means the tools do not exist rather than existing
        # and failing - the same graceful-skip posture the index and the
        # OMOP layer already use.
        analytics_connection = identity_connection = None
        if settings.analytics_configured():
            analytics_connection = lambda: connect(  # noqa: E731
                settings, settings.omop_analyst_username
            )
        if settings.identity_search_configured():
            identity_connection = lambda: connect(  # noqa: E731
                settings, settings.identity_reader_username
            )

        # Cross-record research - the clinical retrieval index's read
        # role (core/db/retrieval_schema.sql; read its header before
        # provisioning). Same graceful-skip posture as the two above.
        research_search_connection = None
        if settings.retrieval_search_configured():
            research_search_connection = lambda: connect(  # noqa: E731
                settings, settings.retrieval_search_username
            )

        # Psychotherapy pieces: wired only when the assistant's own
        # psychotherapy gate is acknowledged AND the deployment
        # provisioned the separate psychotherapy retrieval role. Built
        # here, at startup, so a misconfigured psychotherapy store fails
        # the deployment loudly rather than failing the first
        # psychotherapy question quietly - the same refuse-to-start
        # posture the assistant itself uses for missing acknowledgements.
        assistant_settings = settings_from_env()
        psychotherapy_search_connection = psychotherapy_reader = None
        if assistant_settings is not None and assistant_settings.psychotherapy_access:
            if settings.psychotherapy_retrieval_configured():
                psychotherapy_search_connection = lambda: connect(  # noqa: E731
                    settings, settings.retrieval_psych_username
                )
            from core.crypto.envelope import EnvelopeEncryptor as _Encryptor
            from core.fhir.restore_common import restore_one
            from core.storage.factory import build_psychotherapy_storage

            psych_storage = build_psychotherapy_storage(settings)
            psych_encryptor = _Encryptor(
                kms=build_kms(settings, key_id=settings.psychotherapy_kms_key_id)
            )
            psychotherapy_reader = lambda key: restore_one(  # noqa: E731
                psych_storage, psych_encryptor, key
            )

        # Telemetry (optional): every interaction's metrics, and the ops
        # page's summaries. Fire-and-forget on the write path - see
        # core/assistant/telemetry.py.
        ops_connection = None
        if settings.assistant_ops_configured():
            ops_connection = lambda: connect(  # noqa: E731
                settings, settings.assistant_ops_username
            )

        app.state.assistant = assistant_runtime.build(
            assistant_settings=assistant_settings,
            platform_settings=settings,
            profile=profile_from_env(),
            reader=reader,
            analytics_connection=analytics_connection,
            identity_connection=identity_connection,
            research_search_connection=research_search_connection,
            psychotherapy_search_connection=psychotherapy_search_connection,
            psychotherapy_reader=psychotherapy_reader,
            ops_connection=ops_connection,
        )

    # Release of information connects as the SAME role the reader does,
    # and that is a deliberate choice rather than a shortcut. On GCP a
    # Postgres username is the service account's own email (see
    # deploy/gcp/database.tf), so a dedicated `phi_ai_roi` role is
    # not portable across the three clouds - it is the same Cloud SQL IAM
    # constraint README.md already names as a real architectural
    # difference. What keeps role separation meaningful is that the
    # reader's grants on stored_resources are unchanged and still
    # SELECT-only: the record index cannot be mutated from here. The
    # roi_requests table is workflow state, not the index, and it is
    # granted SELECT/INSERT/UPDATE - never DELETE, because those rows are
    # the accounting of disclosures under 45 CFR 164.528 and a disclosure
    # that can be erased is not an accounting. See core/db/schema.sql and
    # each cloud's bootstrap SQL.
    app.state.roi = ROIService(
        connection_factory=lambda: connect(settings, settings.db_reader_username),
        storage=storage,
        encryptor=encryptor,
        audit=audit,
        reader=reader,
    )
    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    host = env_var("WEB_HOST", "127.0.0.1") or "127.0.0.1"
    port = int(env_var("WEB_PORT", "8080") or "8080")

    if host not in ("127.0.0.1", "localhost", "::1"):
        local = (env_var("WEB_LOCAL_ACCOUNTS", "") or "").strip().lower() in (
            "1", "true", "yes"
        )
        logging.getLogger("phi-ai.web").warning(
            "binding to %s, which is reachable off-host. %s",
            host,
            "This deployment verifies credentials itself (local accounts) - confirm it "
            "is behind TLS and reachable only where you intend."
            if local else
            "This application performs NO authentication of its own - confirm an "
            "authenticating proxy is the only route to it.",
        )

    try:
        app = build()
    except Exception as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 1

    import uvicorn

    uvicorn.run(app, host=host, port=port, access_log=False)  # access_log off: paths carry ids
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# Made by Ryan Gomez & Co. Inc.
