# ---------------------------------------------------------------------------
# Postgres index: queryable metadata derived from the S3 store.
#
# S3 stays the system of record - authoritative over this database in any
# disagreement, and the thing the index is rebuilt FROM. That is authority,
# NOT immutability: this stack provisions no S3 Object Lock, so nothing at
# the storage layer prevents a stored object from being changed or
# deleted by a principal with the IAM permission to do so (see
# s3_store.tf's header). This database holds structural metadata
# only - resource type, S3 key, hash, timestamps, and Epic's own opaque
# internal patient reference. No clinical content ever reaches it - see
# core/db/schema.sql for the reasoning and the hard rule.
#
# Uses the account's DEFAULT VPC and its default subnets rather than
# provisioning a new VPC. That is a deliberate cost decision, not
# laziness: a new VPC needing outbound internet access for anything would
# need a NAT Gateway, which runs ~$32/month plus data processing charges -
# more than every other line item in this stack combined, including RDS
# itself during the free-tier window. The default VPC already exists at
# no cost in every AWS account.
# ---------------------------------------------------------------------------

data "aws_vpc" "default" {
  count   = var.enable_db ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.enable_db ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
}

# RDS requires a subnet group spanning at least two AZs even for a
# single-AZ instance (it reserves the option to fail over). The default
# VPC's default subnets already span every AZ in the region.
resource "aws_db_subnet_group" "index" {
  count      = var.enable_db ? 1 : 0
  name       = "${local.name_prefix}-index"
  subnet_ids = data.aws_subnets.default[0].ids

  tags = {
    Name = "${local.name_prefix}-index-subnet-group"
  }
}

# ---------------------------------------------------------------------------
# Security group: no ingress by default.
#
# A database with an open security group is a bigger exposure than a
# similarly-misconfigured S3 bucket - a network path is all that stands
# between an attacker and every row, versus S3's additional layers
# (bucket policy, IAM, object-level ACLs). db_allowed_cidr_blocks
# defaults to empty specifically so this has to be an explicit,
# deliberate choice, not an accidental default.
# ---------------------------------------------------------------------------

resource "aws_security_group" "index_db" {
  count       = var.enable_db ? 1 : 0
  name        = "${local.name_prefix}-index-db"
  description = "PHI AI Platform Postgres index - inbound 5432 only from explicitly allowed CIDRs"
  vpc_id      = data.aws_vpc.default[0].id

  lifecycle {
    precondition {
      condition     = !contains(var.db_allowed_cidr_blocks, "0.0.0.0/0")
      error_message = "db_allowed_cidr_blocks must not include 0.0.0.0/0. Scope to a specific IP (/32) or your VPC's CIDR instead - see the variable description in variables.tf."
    }
  }

  tags = {
    Name = "${local.name_prefix}-index-db"
  }
}

resource "aws_vpc_security_group_ingress_rule" "index_db" {
  for_each = var.enable_db ? toset(var.db_allowed_cidr_blocks) : toset([])

  security_group_id = aws_security_group.index_db[0].id
  cidr_ipv4   = each.value
  from_port   = 5432
  to_port     = 5432
  ip_protocol = "tcp"
  description = "Postgres from an explicitly allowed CIDR"
}

resource "aws_vpc_security_group_egress_rule" "index_db" {
  count = var.enable_db ? 1 : 0

  security_group_id = aws_security_group.index_db[0].id
  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"
  description = "RDS default egress - the instance itself does not initiate outbound connections beyond AWS service traffic"
}

# ---------------------------------------------------------------------------
# Master credential: created once by Terraform, used once by an operator
# to bootstrap the application roles (core/db/bootstrap_aws.sql), then
# never touched again. The running application authenticates via IAM
# database auth tokens (core/db/connection.py) - it never reads this
# password. Deliberately NOT using RDS-managed master password (Secrets
# Manager) to avoid the extra ~$0.40/month for a credential that's used
# exactly once; the trade-off is that the value lives in Terraform state
# instead, which is already SSE-encrypted and access-restricted (see
# deploy/aws/bootstrap/main.tf).
# ---------------------------------------------------------------------------

resource "random_password" "db_master" {
  count            = var.enable_db ? 1 : 0
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_instance" "index" {
  count = var.enable_db ? 1 : 0

  identifier     = "${local.name_prefix}-index"
  engine         = "postgres"
  engine_version = "16"

  instance_class    = var.db_instance_class
  allocated_storage = var.db_allocated_storage_gb
  storage_type      = var.db_storage_type
  storage_encrypted = true
  kms_key_id        = local.db_key_arn

  # Both of these are ForceNew in the AWS provider: RDS's
  # ModifyDBInstance API cannot rename a database or a master user in
  # place, so changing either one after the first apply plans DESTROY +
  # CREATE of the whole instance. Pick them before the first apply.
  #
  # They must agree with core/db/bootstrap_aws.sql, which is run once as
  # this master user against this database, and with the rds-db:connect
  # ARNs in iam.tf, which name the phi_ai_* roles that file creates. A
  # mismatch anywhere in that set is an authentication failure at IAM,
  # before Postgres is ever reached.
  db_name  = "phi_ai_index"
  username = "phi_ai_master"
  password = random_password.db_master[0].result

  db_subnet_group_name   = aws_db_subnet_group.index[0].name
  vpc_security_group_ids = [aws_security_group.index_db[0].id]
  publicly_accessible    = var.db_publicly_accessible

  multi_az = var.db_multi_az

  # IAM database authentication: the running application never uses the
  # master password above. See core/db/connection.py and
  # core/db/bootstrap_aws.sql.
  iam_database_authentication_enabled = true

  backup_retention_period = var.db_backup_retention_days
  deletion_protection     = var.db_deletion_protection
  skip_final_snapshot     = var.environment == "dev" && var.db_skip_final_snapshot
  final_snapshot_identifier = var.environment == "dev" && var.db_skip_final_snapshot ? null : "${local.name_prefix}-index-final"

  # Immediate application, not the default maintenance window, so a dev
  # stack's settings take effect right away rather than waiting for the
  # next window - acceptable for dev, reconsider for a real deployment
  # where an unplanned-feeling restart during business hours is a problem.
  apply_immediately = var.environment == "dev"

  lifecycle {
    precondition {
      condition     = var.environment == "dev" || var.db_deletion_protection
      error_message = "db_deletion_protection must be true outside dev."
    }
    precondition {
      condition     = !var.db_multi_az || var.environment != "dev"
      error_message = "db_multi_az should stay false in dev - it doubles RDS free-tier hour consumption for no benefit in a disposable dev stack."
    }
    precondition {
      condition     = var.environment == "dev" || !var.db_publicly_accessible
      error_message = "db_publicly_accessible must be false outside dev. A production index should be reached from within the VPC (application compute, bastion, or VPN), never directly from the internet."
    }

    # Password changes (e.g. someone re-running with a different
    # random_password seed) should never silently force a replacement of
    # a database that might hold real index data.
    #
    # db_name and username are deliberately NOT ignored here. Ignoring
    # them would suppress the DESTROY + CREATE plan a rename produces,
    # but it also permanently blinds Terraform to drift on the two
    # attributes the IAM database auth path depends on - and a silent
    # mismatch there is an outage, not a cosmetic diff.
    ignore_changes = [password]
  }

  tags = {
    Name = "${local.name_prefix}-index"
    Role = "phi-ai-index"
  }
}

locals {
  # Mirrors the audit-key-sharing pattern in kms.tf: reuse the audit key
  # by default (this database is metadata-tier sensitivity, not PHI-tier),
  # or provision a dedicated key if separate_db_key is set.
  db_key_arn = var.enable_db ? (var.separate_db_key ? aws_kms_key.db[0].arn : local.audit_key_arn) : null
}

resource "aws_kms_key" "db" {
  count = var.enable_db && var.separate_db_key ? 1 : 0

  description             = "PHI AI Platform (${var.environment}) - encrypts RDS index storage"
  enable_key_rotation     = var.enable_key_rotation
  deletion_window_in_days = 30

  tags = {
    Name = "${local.name_prefix}-db-key"
    Role = "index-db-encryption"
  }
}
# Made by Ryan Gomez & Co. Inc.
