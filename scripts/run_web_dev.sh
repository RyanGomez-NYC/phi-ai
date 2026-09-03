#!/usr/bin/env bash
# Launch the PHI AI web interface for the dev evaluation deployment:
# .env sourced, running under the tagged restore-role session (see
# scripts/restore_role_credential_process.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

export AWS_CONFIG_FILE="$PWD/deploy/aws/awsconfig-web"
export AWS_PROFILE=phi-ai-web-restore

exec ./.venv/bin/python -m core.web
# Made by Ryan Gomez & Co. Inc.
