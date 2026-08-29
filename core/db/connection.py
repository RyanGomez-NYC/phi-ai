# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
IAM/Microsoft Entra-authenticated connections to the Postgres index,
across all three supported clouds.

No database password is ever generated, stored, or read by the running
application for either application role (phi_ai_ingest,
phi_ai_reader), on any cloud. Instead, each connection is
authenticated with a short-lived credential the cloud's own identity
system issues on request - the same pattern this project already uses
to avoid a shared secret for Epic auth (signed JWT) and cloud storage
access (instance/workload identity, service account impersonation, or
managed identity, depending on cloud). The one password that exists at
all, on any cloud, is the database's own initial administrator
password - created once by Terraform, used once by an operator to run
the appropriate core/db/bootstrap_*.sql for the configured cloud, and
never touched by this module or by either application role afterward.

The three mechanisms genuinely differ, not just in which SDK is called:

  AWS (RDS): a short-lived (15-minute) auth token from
    rds.generate_db_auth_token(), presented as the password to a
    standard psycopg connection over host:port. See _connect_aws().

  GCP (Cloud SQL): the Cloud SQL Python Connector - Google's own
    recommended mechanism ("for the most secure and reliable
    experience") - establishes an encrypted tunnel and handles IAM
    token issuance/refresh internally. No host:port at all; instead an
    "instance connection name" (project:region:instance) identifies the
    target, and the connection is made through pg8000 rather than
    psycopg - the connector does not support psycopg's underlying
    driver (libpq). See _connect_gcp(), and this module's own note
    below on the connector's real username-format requirement.

  Azure (Flexible Server): a Microsoft Entra access token, scoped
    specifically to https://ossrdbms-aad.database.windows.net (NOT the
    general Microsoft Graph scope azure-identity defaults to elsewhere
    in this project - get_token() requires this exact resource value),
    presented as the password to a standard psycopg connection over
    host:port - structurally the closest of the three to AWS's
    approach. See _connect_azure().

core/db/index.py and core/db/reconcile.py never need to know which of
these produced their connection - all three return a DB-API 2.0
compatible object (cursor/execute/commit/rollback/fetchall/description),
which is all that module ever calls. psycopg.Connection and
pg8000.dbapi.Connection both satisfy this, so the abstraction boundary
holds even though the underlying driver differs by cloud.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.config.settings import Settings


def _connect_aws(
    host: str,
    port: int,
    dbname: str,
    username: str,
    region: str,
    connect_timeout: int = 10,
) -> Any:
    """
    AWS RDS IAM database authentication.

    Requires the connecting IAM principal to hold `rds-db:connect`
    scoped to its own database username - see deploy/aws/iam.tf.
    Requires the corresponding Postgres role to have been created with
    `GRANT rds_iam` - see core/db/bootstrap_aws.sql.

    `sslmode=require` is the floor (matches the TLS 1.2+ requirement
    enforced elsewhere in this project); for a hardened deployment, use
    `sslmode=verify-full` with AWS's RDS CA bundle so the client also
    verifies server identity, not just that *some* certificate was
    presented. See runbooks/RUNBOOK_AWS_SETUP.md for the CA bundle
    download.
    """
    import boto3
    import psycopg

    rds_client = boto3.client("rds", region_name=region)
    token = rds_client.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=username, Region=region)

    return psycopg.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=token,
        sslmode="require",
        connect_timeout=connect_timeout,
    )


def _connect_gcp(
    instance_connection_name: str,
    dbname: str,
    username: str,
    connect_timeout: int = 10,
) -> Any:
    """
    GCP Cloud SQL automatic IAM database authentication, via the Cloud
    SQL Python Connector - confirmed as Google's own recommended
    mechanism over manually requesting and presenting an OAuth2 token,
    which is also possible (Cloud SQL's "manual" IAM auth mode) but
    forgoes the connector's automatic token refresh and its encrypted
    tunnel, for no benefit to this project.

    Requires the connecting service account to hold `roles/cloudsql.client`
    (confirmed from the connector library's own documentation) and the
    Cloud SQL instance to have `cloudsql.iam_authentication` enabled -
    see deploy/gcp/database.tf.

    A REAL GOTCHA, worth stating explicitly rather than discovering at
    connect time: the `username` here is NOT a freely-chosen Postgres
    role name the way it is on AWS/Azure. Per Google's own
    documentation, for a service account it must be the service
    account's email address with the trailing `.gserviceaccount.com`
    suffix stripped. deploy/gcp/database.tf's own outputs compute this
    value directly from the ingest/restore service accounts already
    provisioned in identities.tf, specifically so a deployer copies a
    ready-made value into PHI_AI_DB_INGEST_USERNAME /
    PHI_AI_DB_READER_USERNAME rather than needing to derive it by
    hand.

    Returns a pg8000 DB-API connection, not a psycopg one - the
    connector does not support psycopg's underlying driver. See this
    module's own docstring for why core/db/index.py does not need to
    care about this difference.
    """
    from google.cloud.sql.connector import Connector

    connector = Connector()
    return connector.connect(
        instance_connection_name,
        "pg8000",
        user=username,
        db=dbname,
        enable_iam_auth=True,
        timeout=connect_timeout,
    )


def _connect_azure(
    host: str,
    port: int,
    dbname: str,
    username: str,
    connect_timeout: int = 10,
) -> Any:
    """
    Azure Database for PostgreSQL Flexible Server, Microsoft Entra
    (Azure AD) authentication.

    Requires the connecting managed identity to have been registered as
    a Postgres role via `pgaadauth_create_principal_with_oid()` - the
    special function the PGAadAuth extension provides once Entra
    authentication is enabled on the server - see core/db/bootstrap_azure.sql
    and deploy/azure/database.tf.

    The token request MUST scope to
    https://ossrdbms-aad.database.windows.net specifically - confirmed
    from Microsoft's own documentation as the exact resource value
    required for PostgreSQL specifically; this is a different scope
    from the Microsoft Graph default azure-identity's DefaultAzureCredential
    otherwise assumes, and from the Key Vault-specific scope
    core/crypto/envelope.py's AzureKMS implicitly requests via its own
    SDK client. Getting this value wrong produces a token that
    authenticates to Azure fine but is rejected by Postgres, not an
    up-front auth failure - worth being precise about rather than
    guessing at a plausible-looking scope.
    """
    from azure.identity import DefaultAzureCredential
    import psycopg

    credential = DefaultAzureCredential()
    token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default")

    return psycopg.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=token.token,
        sslmode="require",
        connect_timeout=connect_timeout,
    )


def connect(settings: "Settings", username: str, connect_timeout: int = 10) -> Any:
    """
    Open an IAM/Entra-authenticated connection to the Postgres index,
    dispatching to the correct provider-specific mechanism based on
    settings.cloud_provider - mirrors core/storage/factory.py's
    build_storage()/build_kms()/build_audit_sink() pattern, so "which
    cloud am I on" is resolved in one place here too, rather than
    requiring every caller (scheduler.py, bulk_scheduler.py,
    reconcile.py) to branch on the provider itself.

    `username` is the Postgres role connecting - phi_ai_ingest or
    phi_ai_reader in ordinary use (settings.db_ingest_username /
    settings.db_reader_username), passed explicitly by the caller rather
    than read from settings directly, since which one applies depends
    on what the caller is doing, not on the deployment itself.

    Raises ValueError for gcp specifically if
    settings.gcp_cloud_sql_instance_connection_name is not set - unlike
    host/port, which have a natural "just try it and fail at the TCP
    layer" degradation, a missing instance connection name has no
    sensible way to fail other than immediately and clearly.
    """
    if settings.cloud_provider == "aws":
        return _connect_aws(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            username=username,
            region=settings.storage_region,
            connect_timeout=connect_timeout,
        )

    if settings.cloud_provider == "gcp":
        if not settings.gcp_cloud_sql_instance_connection_name:
            raise ValueError(
                "PHI_AI_GCP_CLOUD_SQL_INSTANCE_CONNECTION_NAME must be set to connect to the "
                "Postgres index on GCP - see deploy/gcp/database.tf's own "
                "instance_connection_name output."
            )
        return _connect_gcp(
            instance_connection_name=settings.gcp_cloud_sql_instance_connection_name,
            dbname=settings.db_name,
            username=username,
            connect_timeout=connect_timeout,
        )

    if settings.cloud_provider == "azure":
        return _connect_azure(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            username=username,
            connect_timeout=connect_timeout,
        )

    raise ValueError(f"Unsupported cloud provider: {settings.cloud_provider}")
# Made by Ryan Gomez & Co. Inc.
