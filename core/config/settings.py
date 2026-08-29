# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Deployment configuration.

Loaded from environment variables so it works identically whether run via
docker-compose, a cloud VM, or a Kubernetes deployment - no config file
containing secrets is ever committed or required. The EMR private key is
referenced by file path and mounted read-only into the container (see
docker-compose.yml), the same pattern used for cloud credentials, rather
than passed as an env var - PEM key material in an env var tends to end
up in process listings and log capture more often than a mounted file
does.

ENVIRONMENT VARIABLE PREFIX
---------------------------
Every variable this module reads is named PHI_AI_<SUFFIX>. That is the
only spelling; nothing else is read.

env_var() IS PUBLIC AND IS THE ONLY SUPPORTED WAY TO READ ONE OF THESE
VARIABLES, from this module or any other. A module that calls
os.environ.get() directly grows a second precedence rule that can
disagree with this one - and because nearly every setting here is
optional and defaulted, a disagreement does not raise, it silently
switches a feature off. Call env_var().
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("phi-ai.config")

#: Environment variable prefix for every setting this module reads.
ENV_PREFIX = "PHI_AI_"


class ConfigError(Exception):
    pass


def env_var(suffix: str, default: Optional[str] = None) -> Optional[str]:
    """
    Reads PHI_AI_<suffix> from the environment, or returns `default`
    (None unless given) when it is unset or empty.

    PUBLIC ON PURPOSE. Every module in this project that needs a
    deployment variable calls this, rather than reaching for
    os.environ.get() itself - see the module docstring above for what
    happens when one doesn't.
    """
    return os.environ.get(ENV_PREFIX + suffix) or default


def _require(suffix: str) -> str:
    """
    Reads a required variable, raising a ConfigError naming it in full
    if it is unset - an error message is the most-read documentation
    this project has, so it names the variable an operator has to set,
    spelled exactly as they must spell it.
    """
    val = env_var(suffix)
    if not val:
        raise ConfigError(
            f"Required environment variable {ENV_PREFIX + suffix} is not set"
        )
    return val


def _parse_retention_overrides(raw: str) -> dict[str, int]:
    """
    Parses PHI_AI_RETENTION_YEARS_OVERRIDES - a JSON object mapping
    FHIR resource type to a retention period in years that differs from
    the deployment-wide PHI_AI_RETENTION_YEARS default, e.g.:

        {"DocumentReference": 15, "Immunization": 30}

    Exists because retention requirements genuinely vary by data type and
    jurisdiction, not just by deployment - see docs/COMPLIANCE.md. A
    single flat retention_years value across an entire deployment cannot
    express "this resource type needs a longer period than the rest,"
    which real state law sometimes requires (e.g., immunization records
    are commonly retained longer than general clinical records in
    several states). See core/fhir/client.py for where this is applied.

    For anything beyond a quick manual override, prefer
    PHI_AI_RETENTION_RULESET_PATH instead (see below) - a structured
    file with required citation/reviewer/review-date fields per rule,
    rather than a bare JSON blob with no sourcing attached to it.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{ENV_PREFIX}RETENTION_YEARS_OVERRIDES is not valid JSON: {exc}. "
            'Expected a JSON object like {"DocumentReference": 15, "Immunization": 30}.'
        ) from exc

    if not isinstance(parsed, dict):
        raise ConfigError(
            f"{ENV_PREFIX}RETENTION_YEARS_OVERRIDES must be a JSON object mapping resource "
            f'type to years, e.g. {{"DocumentReference": 15}} - got {type(parsed).__name__}.'
        )

    result: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            years = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{ENV_PREFIX}RETENTION_YEARS_OVERRIDES[{key!r}] = {value!r} is not an integer "
                "number of years."
            ) from exc
        if years < 1:
            raise ConfigError(
                f"{ENV_PREFIX}RETENTION_YEARS_OVERRIDES[{key!r}] = {years} must be at least 1 year."
            )
        result[str(key)] = years

    return result


def _resolve_retention(
) -> tuple[int, dict[str, int], Optional[str]]:
    """
    Determines (retention_years, retention_years_overrides, ruleset_jurisdiction)
    from whichever retention configuration source is present.

    PHI_AI_RETENTION_RULESET_PATH, if set, takes precedence - see
    core/config/retention_rules.py for what that file format is and why
    it exists (required citation/reviewer/review-date per rule, checked
    with `python -m core.config.retention_rules_check`, meant to be
    owned by a Health Information Manager rather than edited as a bare
    env var). This function does not itself decide what any retention
    figure should be - see that module's own docstring for the same
    point, made at more length: this reads and applies a ruleset,
    never determines one.

    Falls back to the flat PHI_AI_RETENTION_YEARS /
    PHI_AI_RETENTION_YEARS_OVERRIDES variables if no ruleset path is
    set, for anyone not using the richer ruleset mechanism.
    """
    ruleset_path = env_var("RETENTION_RULESET_PATH")

    flat_years_set = bool(env_var("RETENTION_YEARS"))
    flat_overrides_set = bool(env_var("RETENTION_YEARS_OVERRIDES"))

    if ruleset_path:
        from core.config.retention_rules import RetentionRulesetError, load_ruleset

        try:
            ruleset = load_ruleset(ruleset_path)
        except RetentionRulesetError as exc:
            raise ConfigError(
                f"{ENV_PREFIX}RETENTION_RULESET_PATH is set to {ruleset_path!r} but that file "
                f"could not be used:\n\n{exc}\n\n"
                f"Run `python -m core.config.retention_rules_check {ruleset_path}` for a "
                "fuller check, or fix the file and try again."
            ) from exc

        if flat_years_set or flat_overrides_set:
            log.warning(
                "Both %sRETENTION_RULESET_PATH and the flat "
                "%sRETENTION_YEARS/%sRETENTION_YEARS_OVERRIDES variables "
                "are set. The ruleset file (%s) takes precedence; the flat variables are "
                "ignored entirely. Remove whichever one you don't intend to use, so it's clear "
                "which is actually in effect.",
                ENV_PREFIX,
                ENV_PREFIX,
                ENV_PREFIX,
                ruleset_path,
            )

        retention_years, retention_years_overrides = ruleset.to_retention_config()
        return retention_years, retention_years_overrides, ruleset.jurisdiction

    retention_years = int(env_var("RETENTION_YEARS", "10") or "10")
    if retention_years < 1:
        raise ConfigError(f"{ENV_PREFIX}RETENTION_YEARS must be at least 1")

    retention_years_overrides = _parse_retention_overrides(
        env_var("RETENTION_YEARS_OVERRIDES", "") or ""
    )
    return retention_years, retention_years_overrides, None


@dataclass(frozen=True)
class Settings:
    cloud_provider: str  # "aws" | "gcp" | "azure"
    retention_years: int

    # PHI object storage - the unconditional system of record. Every
    # other data structure in this project (the Postgres index, the OMOP
    # layer, the imaging index, the identity index) is derived from it
    # and rebuildable; if any of them ever disagree with what is here,
    # this wins.
    storage_bucket: str
    storage_region: str
    kms_key_id: str

    # Audit log storage - deliberately a separate bucket and key from the
    # object store above, so append-only audit access can be granted
    # without also granting decrypt access to PHI. See deploy/aws/iam.tf.
    audit_bucket: str
    audit_kms_key_id: str


    # Per-resource-type retention overrides, in years - see
    # _parse_retention_overrides() / _resolve_retention() above and
    # docs/COMPLIANCE.md for why a single flat retention period across
    # the whole deployment isn't always sufficient. Empty by default:
    # every resource type uses the flat retention_years above unless
    # explicitly listed here.
    retention_years_overrides: dict[str, int] = field(default_factory=dict)

    # Set only when retention configuration came from a
    # PHI_AI_RETENTION_RULESET_PATH file (core/config/retention_rules.py)
    # rather than the flat env vars - the jurisdiction that ruleset file
    # declared itself to be for. None when using the flat variables, or
    # when no jurisdiction context applies.
    retention_ruleset_jurisdiction: Optional[str] = None

    # Psychotherapy notes (optional) - a genuinely separate storage
    # boundary, not a flag within the general store. See
    # deploy/aws/s3_psychotherapy.tf, deploy/aws/kms.tf, and
    # runbooks/RUNBOOK_PSYCHOTHERAPY_NOTES.md for why: 45 CFR
    # 164.508(a)(2) requires authorization for nearly any use or
    # disclosure of psychotherapy notes, and HIPAA's own definition
    # requires they be maintained in "a different location," not just
    # distinguished by metadata within the same store. Left unset by
    # default - core/fhir/client.py's psychotherapy write path fails
    # loudly rather than silently falling back to the general store if
    # these aren't configured but are needed.
    psychotherapy_storage_bucket: Optional[str] = None
    psychotherapy_kms_key_id: Optional[str] = None

    # Which EMR vendor the FHIR connection below points at - a key into
    # core/fhir/emr_profiles.PROFILES ("epic", "cerner", "athenahealth",
    # "eclinicalworks", "meditech", "nextgen"). Selects the vendor's
    # capability profile (auth flow, supported resources, bulk export,
    # paging); validated at load time so a typo fails at startup, next to
    # its cause, rather than as a confusing auth error mid-run.
    emr_vendor: str = "epic"

    # Source EMR FHIR connection - see docs/EMR_CONNECTORS.md for the
    # per-vendor registration and auth details. For SMART Backend
    # Services vendors this is the RS384 JWT client-assertion flow, not a
    # client secret.
    fhir_base_url: str = ""
    fhir_token_url: str = ""
    fhir_client_id: str = ""
    fhir_private_key_pem: bytes = b""
    fhir_jwt_kid: Optional[str] = None

    # Client secret, for the one vendor whose auth model is a secret
    # rather than a signed JWT assertion (athenahealth - see
    # core/fhir/emr_profiles.py, auth_flow="oauth2_client_credentials").
    # Inert for every other vendor: Epic and the other SMART Backend
    # Services targets never accept one, which is why from_env() requires
    # it exactly when the selected vendor's profile says a secret is the
    # flow, and ignores it otherwise. See
    # FHIRIngestionClient.authenticate_from_settings().
    fhir_client_secret: Optional[str] = None

    # Bulk Data Export (Group-level) - see core/fhir/bulk_client.py's
    # module docstring for the full citations from Epic's own docs.
    # fhir_group_id is NOT discoverable through any API - it has to come
    # from Epic directly (email openepic@epic.com for a sandbox one) or
    # from the healthcare organization in a real deployment. Left unset
    # by default; core/fhir/bulk_scheduler.py fails with a clear error
    # rather than a confusing one if it's needed but missing.
    fhir_group_id: Optional[str] = None
    # Epic's own recommendation for groups of 100 or fewer patients (see
    # bulk_client.py) - raise for larger groups.
    bulk_poll_interval_seconds: int = 600
    # Ceiling before bulk_scheduler.py gives up waiting and fails loudly.
    # Epic itself allows up to 14 days before deleting results, so this
    # is a configured operational ceiling, not Epic's own limit.
    bulk_max_wait_seconds: int = 4 * 60 * 60

    # Postgres index (optional), with genuine parity across all three
    # clouds - see core/db/connection.py's module docstring for the full
    # account of each cloud's own IAM/Entra authentication mechanism.
    # The object store remains the system of record regardless (see
    # core/db/schema.sql); if db_target_configured() is false, indexing
    # is simply skipped and ingestion works identically either way. No
    # password field, anywhere, on any cloud: every connection
    # authenticates via a short-lived credential generated at connect
    # time (core/db/connection.py), never a stored credential.
    #
    # db_host/db_port apply to AWS and Azure, both of which connect over
    # a direct host:port. GCP does not use either - Cloud SQL's Python
    # Connector instead takes an instance connection name
    # (gcp_cloud_sql_instance_connection_name below) and establishes its
    # own encrypted tunnel; db_host/db_port are left unset on GCP.
    db_host: Optional[str] = None
    db_port: int = 5432
    db_name: Optional[str] = None
    db_ingest_username: Optional[str] = None
    db_reader_username: Optional[str] = None

    # Provider-specific extras
    gcp_project: Optional[str] = None
    azure_account_url: Optional[str] = None
    azure_vault_url: Optional[str] = None

    # GCP Cloud SQL instance connection name (format:
    # "project:region:instance") - see core/db/connection.py's
    # _connect_gcp() and deploy/gcp/database.tf's own
    # instance_connection_name output. Required to connect to the
    # Postgres index on GCP specifically; unused on aws/azure.
    gcp_cloud_sql_instance_connection_name: Optional[str] = None

    # OMOP CDM analytics layer (optional, opt-in) - see
    # core/db/omop_schema.sql, core/db/omop_bootstrap_aws.sql, and
    # core/db/omop_etl.py. A genuinely separate feature from the
    # lightweight stored_resources index above: sharing the same
    # underlying database connection mechanism (db_target_configured()
    # below covers both), but requiring its own username - the omop_etl
    # role (core/db/omop_bootstrap_aws.sql), distinct from
    # db_ingest_username's phi_ai_ingest - since the OMOP layer
    # holds identified PHI and needs its own, separately-audited access
    # path rather than reusing the metadata-only index's role. Left
    # unset by default; omop_target_configured() below reports False and
    # core/fhir/scheduler.py/bulk_scheduler.py simply skip OMOP writes
    # entirely when it is, the same graceful-skip posture as the index
    # itself.
    omop_etl_username: Optional[str] = None

    # Disposition role's Postgres username (2026-08-17 audit, C4) - the
    # phi_ai_disposition role (core/db/bootstrap_aws.sql,
    # core/db/omop_bootstrap_aws.sql) core/fhir/purge.py connects as to
    # delete the index row and any OMOP row for a disposed resource, in
    # the same disposal operation as the storage delete. Independently
    # configurable, matching db_ingest_username/db_reader_username/
    # omop_etl_username's existing pattern, rather than hardcoding the
    # bootstrap SQL's role name into purge.py directly. Left unset by
    # default; disposition_db_configured() below reports False and
    # purge.py skips both derived-row deletes entirely (storage-only
    # disposal), the same graceful-skip posture every other optional
    # database code path in this project already uses.
    disposition_db_username: Optional[str] = None

    # Population analytics (optional). The READ-ONLY OMOP role, distinct
    # from omop_etl_username above which writes it. core/analytics/ and
    # the assistant's cohort tools connect as this, and it is the reason
    # a generated SQL query cannot write anything: omop_analyst holds
    # SELECT on seven cdm tables and vocab.concept and nothing else (see
    # core/db/omop_bootstrap_aws.sql). Unset means the assistant answers
    # no population questions at all, which is the default.
    omop_analyst_username: Optional[str] = None

    # Name search (optional, and the most identifying store in this
    # system - read core/db/identity_schema.sql's header before enabling
    # it). Its own role, separate from omop_analyst: counting patients
    # with a condition and looking up who they are are different
    # privileges, and an organisation may reasonably grant one without
    # the other.
    identity_reader_username: Optional[str] = None

    # DICOM imaging index (optional, opt-in) - see
    # core/db/imaging_schema.sql, which holds IDENTIFYING PHI: patient
    # names, birth dates, accession numbers and study descriptions, in
    # queryable columns. That is unavoidable for imaging and is why it
    # gets its own role rather than reusing the reader's: a DICOMweb
    # worklist (PS3.18 10.6) is a list of patients and dates, so there is
    # no de-identified form of it that a records clerk could use. The
    # same posture as omop_etl_username above - a separate, separately
    # granted, separately audited access path for a store that is
    # materially more identifying than the lightweight resource index.
    #
    # Left unset by default; imaging_target_configured() reports False and
    # the DICOMweb API is not mounted at all, so a deployment that does
    # not ingest imaging never exposes it.
    imaging_db_username: Optional[str] = None

    # Clinical retrieval index (optional, opt-in) - the assistant's
    # cross-record text search. READ core/db/retrieval_schema.sql's
    # header before enabling: the table holds clinical prose in the
    # clear, the broadest derived store in this system. Three roles,
    # same posture as every optional store above:
    #
    #   retrieval_etl_username     writes it (core/db/retrieval_etl.py)
    #   retrieval_search_username  read-only general search - what the
    #                              assistant's research tools connect as
    #   retrieval_psych_username   read-only psychotherapy search, a
    #                              SEPARATE table behind a SEPARATE role
    #                              so general research can never quietly
    #                              include it (see the schema header)
    #
    # All unset by default; the tools simply do not exist until each is
    # configured, the same graceful-skip posture as everything above.
    retrieval_etl_username: Optional[str] = None
    retrieval_search_username: Optional[str] = None
    retrieval_psych_username: Optional[str] = None

    # Assistant telemetry / operations (optional) - the aiops schema
    # (core/db/telemetry_schema.sql: metrics and usernames, never
    # content). One INSERT+SELECT role: the web worker records every
    # assistant interaction through it and the ops page reads the
    # summaries back. Unset means no telemetry is recorded and the ops
    # page reports itself unconfigured - the assistant itself is
    # unaffected either way.
    assistant_ops_username: Optional[str] = None

    def db_target_configured(self) -> bool:
        """
        Whether enough deployment-level configuration is present to
        attempt a Postgres index connection at all - i.e. whether a
        database has been provisioned and pointed to, independent of
        which application role (ingest/reader) a specific caller is
        about to connect as.

        FOUND AND FIXED alongside adding genuine GCP/Azure database
        parity: core/fhir/scheduler.py, bulk_scheduler.py, and
        core/db/reconcile.py previously checked `settings.db_host`
        directly to decide whether to attempt a connection - correct for
        AWS and Azure, but always false for a correctly-configured GCP
        deployment, since GCP's Cloud SQL Connector uses an instance
        connection name instead of a host at all (see
        core/db/connection.py's module docstring). A GCP deployment with
        the Postgres index genuinely enabled would have silently skipped
        indexing on every run, with no error - functionally identical to
        the audit-sink wiring gap found and fixed earlier for
        scheduler.py/bulk_scheduler.py, just one layer deeper. Centralized
        here, once, rather than duplicating provider-conditional logic
        in every caller - the same "single place for provider branching"
        principle core/storage/factory.py's own docstring states for
        build_storage()/build_kms()/build_audit_sink().

        Callers additionally check their own required username field
        (db_ingest_username or db_reader_username) themselves - that
        part needs no provider branching, so it isn't folded into this
        method.
        """
        if not self.db_name:
            return False
        if self.cloud_provider == "gcp":
            return bool(self.gcp_cloud_sql_instance_connection_name)
        return bool(self.db_host)

    def omop_target_configured(self) -> bool:
        """
        Whether enough configuration is present to attempt an OMOP
        analytics-layer connection - the same underlying database target
        as the lightweight index (db_target_configured() above) PLUS
        omop_etl_username specifically. The OMOP layer is a genuinely
        separate, opt-in feature layered on the same Postgres instance,
        not automatically enabled just because the lightweight index is
        - a deployment can have db_target_configured() true and this
        false (index only, no OMOP), but never the reverse, since OMOP
        has no independent connection target of its own to configure.

        Also used by core/fhir/purge.py as the signal for "does this
        deployment's Postgres have the OMOP schema/roles set up at
        all" before attempting to delete an OMOP row on disposal - a
        deployment running the OMOP scheduler wiring already has this
        true, so reusing it avoids a second, parallel "is OMOP enabled"
        flag.
        """
        return self.db_target_configured() and bool(self.omop_etl_username)

    def analytics_configured(self) -> bool:
        """Whether population analytics can run.

        Requires the OMOP layer to exist AND a read-only analyst role.
        A deployment with omop_etl_username but no analyst role has the
        data and no way to query it, which is a real state - the ETL can
        be running for a downstream warehouse while this deployment
        exposes nothing.
        """
        return self.omop_target_configured() and bool(self.omop_analyst_username)

    def identity_search_configured(self) -> bool:
        """Whether name search can run.

        Deliberately NOT implied by analytics_configured(). The identity
        index is the one place a patient's name is stored outside an
        encrypted object, so enabling cohort counts must not silently
        enable looking people up by name.
        """
        return self.db_target_configured() and bool(self.identity_reader_username)

    def disposition_db_configured(self) -> bool:
        """
        Whether enough configuration is present for core/fhir/purge.py
        to open a disposition-role Postgres connection - the same
        underlying database target as the lightweight index
        (db_target_configured() above) PLUS disposition_db_username
        specifically, mirroring omop_target_configured()'s own shape.

        A deployment can have the index and/or OMOP configured without
        this (disposition then skips both derived-row deletes and
        removes only the storage object - a storage-only disposal, the
        same graceful-skip posture used everywhere else in this project
        when an optional database feature isn't configured), but this
        being true with db_target_configured() false is not a real
        state - there is no disposition-specific connection target
        independent of the shared database target.
        """
        return self.db_target_configured() and bool(self.disposition_db_username)

    def retrieval_configured(self) -> bool:
        """Whether the retrieval ETL can write the clinical text index -
        the shared database target PLUS its own writer role, the same
        shape as omop_target_configured()."""
        return self.db_target_configured() and bool(self.retrieval_etl_username)

    def retrieval_search_configured(self) -> bool:
        """Whether the assistant's cross-record search can run. Its own
        read role, deliberately not implied by retrieval_configured():
        an organisation can build the index for a downstream use without
        exposing search in the assistant, and vice-versa cannot happen -
        searching an index nobody writes returns nothing, harmlessly."""
        return self.db_target_configured() and bool(self.retrieval_search_username)

    def psychotherapy_retrieval_configured(self) -> bool:
        """Whether psychotherapy text search can run. Separate from the
        general search role for the reason core/db/retrieval_schema.sql's
        header states: no widening of general research access may
        quietly include this table. The assistant additionally requires
        its own acknowledged configuration gate on top of this
        (core/assistant/config.py) and the caller the `psychotherapy`
        role - three independent switches, all deliberate."""
        return self.db_target_configured() and bool(self.retrieval_psych_username)

    def assistant_ops_configured(self) -> bool:
        """Whether assistant telemetry can be written and the ops page
        can read it - the shared database target PLUS the aiops role."""
        return self.db_target_configured() and bool(self.assistant_ops_username)

    def imaging_target_configured(self) -> bool:
        """Whether the DICOM imaging index can be reached.

        The same shape as omop_target_configured(): the shared database
        target PLUS this feature's own username. A deployment can write
        DICOM objects to storage with this unset - the imaging is safe and
        retrievable by key - but nothing can search it and the viewer has
        nothing to talk to, so the DICOMweb API stays unmounted rather
        than serving empty results.
        """
        return self.db_target_configured() and bool(self.imaging_db_username)

    @classmethod
    def from_env(cls) -> "Settings":
        provider = _require("CLOUD_PROVIDER").lower()
        if provider not in ("aws", "gcp", "azure"):
            raise ConfigError(
                f"{ENV_PREFIX}CLOUD_PROVIDER must be one of aws/gcp/azure, got {provider!r}"
            )

        retention_years, retention_years_overrides, retention_ruleset_jurisdiction = _resolve_retention()

        key_path = _require("FHIR_PRIVATE_KEY_PATH")
        key_file = Path(key_path)
        if not key_file.is_file():
            raise ConfigError(
                f"{ENV_PREFIX}FHIR_PRIVATE_KEY_PATH points to {key_path!r}, which does not "
                "exist in this container. Check the docker-compose.yml volume mount - the "
                "private key must be mounted in, never baked into the image."
            )
        private_key_pem = key_file.read_bytes()

        emr_vendor = (env_var("EMR_VENDOR") or "epic").strip().lower()
        # Import here, not at module top: settings must stay importable
        # without dragging the FHIR layer in, but an unknown vendor key
        # must still fail at startup rather than mid-run.
        from core.fhir.emr_profiles import PROFILES

        if emr_vendor not in PROFILES:
            raise ConfigError(
                f"{ENV_PREFIX}EMR_VENDOR must be one of {', '.join(sorted(PROFILES))}; "
                f"got {emr_vendor!r}"
            )

        # Vendor-conditional, validated here so a client-secret vendor
        # with no secret fails at startup next to its cause, not as a 400
        # from the vendor's token endpoint mid-run - the same reasoning
        # as the vendor-key check above.
        fhir_client_secret = env_var("FHIR_CLIENT_SECRET") or None
        if PROFILES[emr_vendor].auth_flow == "oauth2_client_credentials" and not fhir_client_secret:
            raise ConfigError(
                f"{ENV_PREFIX}EMR_VENDOR={emr_vendor!r} authenticates with a client "
                f"secret, but {ENV_PREFIX}FHIR_CLIENT_SECRET is not set. This vendor "
                "does not accept a signed JWT assertion - see its profile notes in "
                "core/fhir/emr_profiles.py and docs/EMR_CONNECTORS.md."
            )

        return cls(
            cloud_provider=provider,
            emr_vendor=emr_vendor,
            retention_years=retention_years,
            retention_years_overrides=retention_years_overrides,
            retention_ruleset_jurisdiction=retention_ruleset_jurisdiction,
            storage_bucket=_require("STORAGE_BUCKET"),
            storage_region=_require("STORAGE_REGION"),
            kms_key_id=_require("KMS_KEY_ID"),
            audit_bucket=_require("AUDIT_BUCKET"),
            audit_kms_key_id=_require("AUDIT_KMS_KEY_ID"),
            psychotherapy_storage_bucket=env_var("PSYCHOTHERAPY_STORAGE_BUCKET") or None,
            psychotherapy_kms_key_id=env_var("PSYCHOTHERAPY_KMS_KEY_ID") or None,
            fhir_base_url=_require("FHIR_BASE_URL"),
            fhir_token_url=_require("FHIR_TOKEN_URL"),
            fhir_client_id=_require("FHIR_CLIENT_ID"),
            fhir_private_key_pem=private_key_pem,
            fhir_jwt_kid=env_var("FHIR_JWT_KID") or None,
            fhir_client_secret=fhir_client_secret,
            fhir_group_id=env_var("FHIR_GROUP_ID") or None,
            bulk_poll_interval_seconds=int(env_var("BULK_POLL_INTERVAL_SECONDS", "600") or "600"),
            bulk_max_wait_seconds=int(
                env_var("BULK_MAX_WAIT_SECONDS", str(4 * 60 * 60)) or str(4 * 60 * 60)
            ),
            db_host=env_var("DB_HOST") or None,
            db_port=int(env_var("DB_PORT", "5432") or "5432"),
            db_name=env_var("DB_NAME") or None,
            db_ingest_username=env_var("DB_INGEST_USERNAME") or None,
            db_reader_username=env_var("DB_READER_USERNAME") or None,
            gcp_project=env_var("GCP_PROJECT"),
            azure_account_url=env_var("AZURE_ACCOUNT_URL"),
            azure_vault_url=env_var("AZURE_VAULT_URL"),
            gcp_cloud_sql_instance_connection_name=env_var(
                "GCP_CLOUD_SQL_INSTANCE_CONNECTION_NAME"
            )
            or None,
            omop_etl_username=env_var("OMOP_ETL_USERNAME") or None,
            disposition_db_username=env_var("DISPOSITION_DB_USERNAME") or None,
            omop_analyst_username=env_var("OMOP_ANALYST_USERNAME") or None,
            identity_reader_username=env_var("IDENTITY_READER_USERNAME") or None,
            imaging_db_username=env_var("IMAGING_DB_USERNAME") or None,
            retrieval_etl_username=env_var("RETRIEVAL_ETL_USERNAME") or None,
            retrieval_search_username=env_var("RETRIEVAL_SEARCH_USERNAME") or None,
            retrieval_psych_username=env_var("RETRIEVAL_PSYCH_USERNAME") or None,
            assistant_ops_username=env_var("ASSISTANT_OPS_USERNAME") or None,
        )
# Made by Ryan Gomez & Co. Inc.
